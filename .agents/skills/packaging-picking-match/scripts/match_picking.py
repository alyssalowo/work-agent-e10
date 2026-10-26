#!/usr/bin/env python3
"""Read-only picking planner; local marker history and scoped verification."""

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from types import MappingProxyType
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

GOOD = "量产/研发交付裸机良品仓"
MACHINE, MATERIAL, MARKER = "机器领料单号", "物料领料单号", "是否加急"
SOURCE = ("领料申请单明细_工单单号", "申请单号", "领料申请单明细_仓库名称", "领料申请单明细_材料品号")
EXCLUSION_IDENTITY = (('单号',),('单身来源','单身来源单号'),('品号','销货明细_品号'),
                      ('特征码','销货明细_特征码'),('数量','销货明细_业务数量'),('包装工单',))


def exclusion_fields(identity):
    if not isinstance(identity,dict): raise ValueError('排除行身份格式不符')
    chosen=[]
    for aliases in EXCLUSION_IDENTITY:
        found=[name for name in aliases if name in identity]
        if len(found)!=1: raise ValueError('排除行身份格式不符')
        chosen.append(found[0])
    if set(identity)!=set(chosen): raise ValueError('排除行身份含未知字段')
    return tuple(chosen)
_IDS_PATTERN = re.compile(r"[\r\n/]+")


def text(value):
    if type(value) is str:
        return value.replace("\r\n", "\n").replace("\r", "\n") if "\r" in value else value
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def ident(value):
    return text(value).strip()


def ids(value):
    return list(dict.fromkeys(p.strip() for p in _IDS_PATTERN.split(text(value)) if p.strip()))


def rectangle(rows):
    width = max((len(row) for row in rows), default=0)
    return [[text(v) for v in row] + [""] * (width-len(row)) for row in rows]


def canonical_id(value):
    value = value.strip()
    parts = urlsplit(value)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", "")) if parts.scheme in ("http", "https") else value


def column_name(index):
    out = ""
    while index:
        index, remainder = divmod(index-1, 26)
        out = chr(65+remainder) + out
    return out


def read_tables(path, sheets=None):
    path = Path(path)
    if path.suffix.lower() in (".tsv", ".csv"):
        if sheets:
            raise ValueError("TSV/CSV 不接受 XLSX 工作表选择参数")
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return {path.stem: rectangle(list(csv.reader(stream, delimiter="\t" if path.suffix.lower()==".tsv" else ",")))}
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        raise ValueError("支持 XLSX/XLSM/TSV/CSV；旧 XLS 须先另存为 XLSX")
    from openpyxl import load_workbook
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        result = {}
        for name in sheets or workbook.sheetnames:
            if name not in workbook.sheetnames:
                raise ValueError("找不到工作表："+name)
            sheet = workbook[name]
            sheet.reset_dimensions()
            rows = []
            for cells in sheet.iter_rows():
                row = []
                for cell in cells:
                    value = cell.value
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 and float(value).is_integer() and re.fullmatch(r"0+", cell.number_format or ""):
                        row.append(str(int(value)).zfill(len(cell.number_format)))
                    else:
                        row.append(text(value))
                rows.append(row)
            width = max((len(row) for row in rows), default=0)
            result[name] = [row + [""] * (width-len(row)) for row in rows]
        return result
    finally:
        workbook.close()


def header(rows, fields, search=20):
    for offset, row in enumerate(rows[:search]):
        names = [ident(v) for v in row]
        if all(name in names for name in fields):
            if any(names.count(name)!=1 for name in fields):
                raise ValueError("匹配表头字段重复")
            return offset, {name: names.index(name) for name in fields}
    return None


def validate_exclusion_rows(rows, sheet, row_offset, records):
    rows=rectangle(rows)
    result={}
    for item in records:
        if item.get('sheet')!=sheet: continue
        number=item.get('row'); identity=item.get('identity')
        if type(number) is not int or number<1 or not isinstance(identity,dict):
            raise ValueError('排除行身份格式不符')
        fields=exclusion_fields(identity)
        found=header(rows,fields,20)
        if not found: raise ValueError('排除行身份表头缺失')
        cols=found[1]
        offset=number-row_offset-1
        if offset<0 or offset>=len(rows) or (found and offset<=found[0]): raise ValueError('排除行不在目标快照范围')
        actual={k:rows[offset][cols[k]] for k in fields}
        if actual!=identity: raise ValueError('排除行身份变化：%s!%s'%(sheet,number))
        if number in result: raise ValueError('排除行重复')
        result[number]=dict(item)
    return result


def source_index(tables, search=20, explicit=False):
    requests = defaultdict(set)
    finished = defaultdict(list)
    counts = Counter()
    used = []
    void_excluded=[]; status_counts=Counter(); status_sheets=set()
    for name, rows in tables.items():
        found = header(rows, SOURCE, search)
        if not found:
            if explicit:
                raise ValueError("源工作表缺少完整表头："+name)
            continue
        used.append(name)
        head, cols = found
        source_columns = tuple(cols[f] for f in SOURCE)
        header_names=[ident(v) for v in rows[head]]
        status_columns=[i for i,v in enumerate(header_names) if v=='APPROVAL_STATUS']
        if len(status_columns)>1: raise ValueError('APPROVAL_STATUS列重复：'+name)
        status_col=status_columns[0] if status_columns else None
        if status_col is not None: status_sheets.add(name)
        for number, row in enumerate(rows[head+1:], head+2):
            if not any(ident(v) for v in row):
                continue
            status=ident(row[status_col]) if status_col is not None and status_col<len(row) else ''
            if status_col is not None:
                status_counts[status]+=1
                if status=='V':
                    void_excluded.append({'sheet':name,'row':number,'APPROVAL_STATUS':status,
                        'WORKORDER_NO':ident(row[source_columns[0]]),'DOC_NO':ident(row[source_columns[1]]),
                        'WAREHOUSE_NAME':ident(row[source_columns[2]]),'ITEM_CODE':ident(row[source_columns[3]]),
                        'ID':ident(row[header_names.index('ID')]) if 'ID' in header_names else '',
                        'DETAIL_ID':ident(row[header_names.index('DETAIL_ID')]) if 'DETAIL_ID' in header_names else ''})
                    counts['void_status_excluded']+=1
                    continue
                if status!='Y':
                    raise ValueError(f'领料源状态未审核或未知，不能作为候选：{name}!{number} APPROVAL_STATUS={status!r}')
                counts['approval_status_y']+=1
            counts["records"] += 1
            order, request, warehouse, product = (ident(row[c]) for c in source_columns)
            if product.startswith("1-"):
                counts["finished_record_mark_only"] += 1
                if order:
                    finished[order].append({"sheet":name,"row":number,"request":request,"warehouse":warehouse,"product":product})
                else:
                    counts["missing_finished_workorder"] += 1
                continue
            if not order or not request or not warehouse:
                counts["missing_fields"] += 1
                continue
            field = MACHINE if warehouse==GOOD else MATERIAL
            if request in requests[(order, field)]:
                counts["duplicate_request_records"] += 1
            requests[(order, field)].add(request)
            counts["eligible_"+field] += 1
    if not used:
        raise ValueError("源文件没有完整领料明细表头")
    if status_sheets and status_sheets != set(used):
        raise ValueError('多工作表领料源APPROVAL_STATUS列不完整')
    stats={"sheets":used, **dict(counts)}
    if status_sheets:
        stats.update(approval_status_counts=dict(status_counts),void_excluded_records=void_excluded)
    return requests, finished, stats


def prepare_source_index(tables, search=20, explicit=False):
    """Build an immutable source index reusable across all sheets in one batch."""
    requests, finished, stats = source_index(tables, search, explicit)
    return (MappingProxyType({key: frozenset(values) for key, values in requests.items()}),
            MappingProxyType({key: tuple(MappingProxyType(dict(value)) for value in values) for key, values in finished.items()}),
            MappingProxyType({**stats, 'sheets': tuple(stats['sheets'])}))


def load_state(path, target_id):
    empty = {"schema_version":1,"target_id":canonical_id(target_id),"markers":[]}
    if not path or not Path(path).exists():
        return empty
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value,dict) or value.get("schema_version")!=1 or canonical_id(value.get("target_id",""))!=empty["target_id"]:
        raise ValueError("处理状态不属于本次文档或版本不受支持")
    value.setdefault("markers",[])
    if not isinstance(value["markers"],list) or any(not isinstance(item,dict) or item.get("status") not in ("submitted","marked","processed") or not item.get("sheet") or not item.get("workorder") for item in value["markers"]):
        raise ValueError("处理状态条目不完整")
    return value


def blocked(state, sheet):
    return {i["workorder"] for i in state["markers"] if i["sheet"]==sheet}


def build_plan(sources, target_rows, sheet, target_id, state=None, workorder_column="包装工单", search=20, row_offset=0, column_offset=0, only_empty=False, explicit_source=False, prepared_index=None, excluded_rows=None):
    if prepared_index is None:
        requests, finished, source_stats = source_index(sources, search, explicit_source)
    else:
        requests, finished, source_stats = prepared_index
        source_stats = dict(source_stats)
        source_stats['sheets'] = list(source_stats['sheets'])
    rows = rectangle(target_rows)
    found = header(rows, (workorder_column, MACHINE, MATERIAL, MARKER), search)
    if not found:
        raise ValueError("目标表缺少包装工单/领料单/是否加急表头")
    head, cols = found
    state = state or load_state(None, target_id)
    skip = blocked(state, sheet)
    expected = [r[:] for r in rows]
    changes, audit, records = [], [], []
    candidate_cache, coverage_issues = {}, []
    counts = Counter()
    excluded_rows=excluded_rows or []
    excluded_index={r['row']:r for r in excluded_rows}
    for offset in range(head+1, len(rows)):
        row = rows[offset]
        if not any(ident(v) for v in row):
            continue
        number = offset + row_offset + 1
        if number in excluded_index:
            continue
        orders = sorted(ids(row[cols[workorder_column]]))
        counts["business_rows"] += 1
        item = {"row":number,"matrix_row":offset,"workorders":orders,"requests":{}}
        for field in (MACHINE, MATERIAL):
            cache_key = (tuple(orders), field)
            if cache_key not in candidate_cache:
                candidate_set = set().union(*(requests.get((order,field),()) for order in orders))
                candidate_cache[cache_key] = (candidate_set, sorted(candidate_set))
            candidate_set, cached_candidates = candidate_cache[cache_key]
            candidates = cached_candidates[:]
            col, before = cols[field], row[cols[field]]
            existing = ids(before)
            existing_set = set(existing)
            missing = sorted(candidate_set-existing_set)
            unknown = sorted(existing_set-candidate_set)
            occupied = bool(ident(before))
            after = before
            if missing and (not occupied or not only_empty):
                # Keep the original string intact; append only new unique IDs.
                after = before + ("/" if occupied and not before.endswith("/") else "") + "/".join(missing) if occupied else "/".join(missing)
            if unknown or (missing and after==before):
                audit.append({"row":number,"field":field,"preserved":before,"missing":missing,"not_in_current_source":unknown})
            item["requests"][field] = {"candidates":candidates,"before":before,"after":after,"added":missing if after!=before else []}
            absent = candidate_set - (set(ids(after)) if after!=before else existing_set)
            if absent:
                coverage_issues.append({"row":number,"field":field,"absent_from_result":sorted(absent)})
            if candidates:
                counts["matched_"+field] += 1
            expected[offset][col] = after
            if after!=before:
                changes.append({"row":number,"matrix_row":offset,"column":col,"field":field,
                                "cell":column_name(col+column_offset+1)+str(number),
                                "before":before,"after":after,"added":missing,"workorders":orders})
                counts["changed_"+field] += 1
                counts["added_request_numbers"] += len(missing)
        eligible_finished = [order for order in orders if order in finished]
        remaining = [order for order in eligible_finished if order not in skip]
        item["finished_workorders"] = eligible_finished
        item["finished_evidence"] = [{"workorder":order,**e} for order in eligible_finished for e in finished[order]]
        before = row[cols[MARKER]]
        if eligible_finished and not remaining:
            item["marker_status"] = "previously_marked_or_processed"
        elif remaining and ident(before):
            item["marker_status"] = "existing_content_preserved"
        elif remaining:
            item["marker_status"] = "write_empty_marker"
            expected[offset][cols[MARKER]] = "内有成品"
            changes.append({"row":number,"matrix_row":offset,"column":cols[MARKER],"field":MARKER,
                            "cell":column_name(cols[MARKER]+column_offset+1)+str(number),
                            "before":before,"after":"内有成品","workorders":orders,"finished_workorders":remaining})
            counts["new_finished_markers"] += 1
        else:
            item["marker_status"] = "no_finished_material"
        records.append(item)
    counts["changed_cells"] = len(changes)
    if coverage_issues and not only_empty:
        raise ValueError("完整计划未包含全部候选，停止写入")
    return {"schema_version":1,"target":{"id":canonical_id(target_id),"sheet":sheet,"row_offset":row_offset,
            "column_offset":column_offset,"header_index":head,"columns":cols,"workorder_column":workorder_column},
            "source_statistics":source_stats,"statistics":dict(counts),"changes":changes,"audit":audit,"rows":records,
            "before_matrix":rows,"expected_matrix":expected,"only_empty":only_empty,
            "excluded_rows":list(excluded_rows),
            "completeness":{"all_candidate_numbers_in_result":not coverage_issues,"preserved_occupied_differences":coverage_issues}}


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n",encoding="utf-8")


def protect(inputs, output):
    if output and Path(output).resolve() in {Path(p).resolve() for p in inputs if p}:
        raise ValueError("输出不能覆盖输入文件")


def one_table(path, sheet=None):
    tables = read_tables(path, [sheet] if sheet else None)
    if len(tables)!=1:
        raise ValueError("目标/快照有多个工作表，请指定工作表")
    return next(iter(tables.items()))


def check(plan, actual, state, before=False):
    actual = rectangle(actual)
    base, wanted, target = plan["before_matrix"], plan["expected_matrix"], plan["target"]
    if len(actual)!=len(base) or (actual and len(actual[0])!=len(base[0])) or actual[target["header_index"]]!=base[target["header_index"]]:
        return {"ok":False,"reason":"range_or_header_changed"}
    differences, accepted = [], []
    order_col = target["columns"][target["workorder_column"]]
    for item in plan["rows"]:
        r = item["matrix_row"]
        if sorted(ids(actual[r][order_col]))!=item["workorders"]:
            differences.append({"row":item["row"],"field":target["workorder_column"],"reason":"workorder_changed"})
    skip = blocked(state, target["sheet"])
    for c in plan["changes"]:
        r, col, value = c["matrix_row"], c["column"], actual[c["matrix_row"]][c["column"]]
        if c["field"]==MARKER and set(c["finished_workorders"]).issubset(skip):
            accepted.append({"cell":c["cell"],"reason":"marker_history_preserved","actual":value})
            continue
        if before:
            valid = value in (c["before"], c["after"])
        else:
            valid = value==c["after"]
        if not valid:
            differences.append({"cell":c["cell"],"field":c["field"],"expected":c["before"] if before else c["after"],"actual":value})
    if not before:
        changed = {(c["matrix_row"],c["column"]) for c in plan["changes"]}
        for item in plan["rows"]:
            r = item["matrix_row"]
            for field in (MACHINE,MATERIAL,MARKER):
                col = target["columns"][field]
                if (r,col) not in changed and actual[r][col]!=base[r][col]:
                    # Preserve collaborator content, but expose the deviation for review.
                    accepted.append({"row":item["row"],"field":field,"reason":"unwritten_content_updated","actual":actual[r][col]})
    return {"ok":not differences,"differences":differences,"accepted_updates":accepted}


def update_history(plan, state, selected_rows=None, actual=None):
    entries = {(e["sheet"],e["workorder"]):e for e in state["markers"]}
    chosen = set(selected_rows) if selected_rows is not None else None
    if chosen is not None and not chosen.issubset({r["row"] for r in plan["rows"]}):
        raise ValueError("处理行号不在计划中")
    for item in plan["rows"]:
        if chosen is not None and item["row"] not in chosen:
            continue
        if chosen is not None and not item["finished_workorders"]:
            raise ValueError("该行没有成品材料记录")
        if actual is not None:
            r = item["matrix_row"]
            if sorted(ids(actual[r][plan["target"]["columns"][plan["target"]["workorder_column"]]]))!=item["workorders"]:
                raise ValueError("标记记录对应工单已变化")
            if actual[r][plan["target"]["columns"][MARKER]]!="内有成品":
                continue
        for order in item["finished_workorders"]:
            key = (plan["target"]["sheet"],order)
            status = "processed" if chosen is not None else "marked"
            if entries.get(key,{}).get("status")=="processed" and status=="marked":
                continue
            entries[key] = {"sheet":key[0],"workorder":key[1],"status":status,"last_row":item["row"]}
    return {**state,"markers":[entries[key] for key in sorted(entries)]}


def record_submitted(plan, state, selected_rows):
    """Remember only explicitly submitted marker changes, without a readback."""
    chosen = set(selected_rows)
    changes = {c["row"]:c for c in plan["changes"] if c["field"]==MARKER}
    if not chosen or not chosen.issubset(changes):
        raise ValueError("提交行号须为计划中实际已提交的成品提示变更行")
    entries = {(e["sheet"],e["workorder"]):e for e in state["markers"]}
    for row in sorted(chosen):
        for order in changes[row]["finished_workorders"]:
            key = (plan["target"]["sheet"],order)
            if entries.get(key,{}).get("status") in ("marked","processed"):
                continue
            entries[key] = {"sheet":key[0],"workorder":key[1],"status":"submitted","last_row":row}
    return {**state,"markers":[entries[key] for key in sorted(entries)]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command",required=True)
    key = sub.add_parser("state-key")
    key.add_argument("--target-id",required=True)
    plan = sub.add_parser("plan")
    for flag in ("source","target","out","target-id"):
        plan.add_argument("--"+flag,required=True)
    plan.add_argument("--source-sheet",action="append")
    plan.add_argument("--target-sheet")
    plan.add_argument("--sheet-name")
    plan.add_argument("--state")
    plan.add_argument("--workorder-column",default="包装工单")
    plan.add_argument("--only-empty",action="store_true")
    plan.add_argument("--header-search-rows",type=int,default=20)
    plan.add_argument("--row-offset",type=int,default=0)
    plan.add_argument("--column-offset",type=int,default=0)
    for command in ("check-before","verify","remember-markers","record-processed","record-submitted"):
        child = sub.add_parser(command)
        child.add_argument("--plan",required=True)
        child.add_argument("--state",required=command in ("remember-markers","record-processed","record-submitted"))
        if command not in ("record-processed","record-submitted"):
            child.add_argument("--snapshot",required=True)
            child.add_argument("--sheet")
        else:
            child.add_argument("--rows",required=True,help="实际已提交提示或用户确认已处理的行号，用逗号分隔")
        if command in ("check-before","verify"):
            child.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        if args.command=="state-key":
            if not canonical_id(args.target_id):
                raise ValueError("文档标识不能空白")
            print(hashlib.sha256(canonical_id(args.target_id).encode("utf-8")).hexdigest())
            return 0
        if args.command=="plan":
            if args.header_search_rows<1 or min(args.row_offset,args.column_offset)<0:
                raise ValueError("表头搜索行数须为正，偏移须非负")
            if not canonical_id(args.target_id):
                raise ValueError("文档标识不能空白")
            protect([args.source,args.target,args.state],args.out)
            state = load_state(args.state,args.target_id)
            name, rows = one_table(args.target,args.target_sheet)
            value = build_plan(read_tables(args.source,args.source_sheet),rows,args.sheet_name or name,args.target_id,
                               state,args.workorder_column,args.header_search_rows,args.row_offset,args.column_offset,
                               args.only_empty,bool(args.source_sheet))
            value.update({"source_file":str(Path(args.source).resolve()),"target_file":str(Path(args.target).resolve()),
                          "state_file":str(Path(args.state).resolve()) if args.state else None})
            save(args.out,value)
            print(json.dumps({"plan":args.out,**value["statistics"],"audit_items":len(value["audit"])},ensure_ascii=False))
            return 0
        value = json.loads(Path(args.plan).read_text(encoding="utf-8-sig"))
        if value.get("schema_version")!=1:
            raise ValueError("计划版本不支持")
        state_path = args.state or value.get("state_file")
        state = load_state(state_path,value["target"]["id"])
        inputs = [args.plan,value.get("source_file"),value.get("target_file")]
        if args.command in ("record-processed","record-submitted"):
            protect(inputs,args.state)
            chosen = [int(x.strip()) for x in args.rows.split(",") if x.strip()]
            if not chosen:
                raise ValueError("处理行号不能为空")
            submitted = args.command=="record-submitted"
            save(args.state,record_submitted(value,state,chosen) if submitted else update_history(value,state,chosen))
            print(json.dumps({"state":args.state,"submitted_rows" if submitted else "processed_rows":chosen},ensure_ascii=False))
            return 0
        _, actual = one_table(args.snapshot,args.sheet)
        if args.command=="remember-markers":
            protect(inputs+[args.snapshot],args.state)
            result = check(value,actual,state)
            if not result["ok"]:
                raise ValueError("先确认快照与计划对应，再记录已保存标记")
            save(args.state,update_history(value,state,actual=actual))
            print(json.dumps({"state":args.state,"saved_marker_records":len(load_state(args.state,value["target"]["id"])["markers"])},ensure_ascii=False))
            return 0
        protect(inputs+[args.snapshot,state_path],args.out)
        report = check(value,actual,state,before=args.command=="check-before")
        if args.out:
            save(args.out,report)
        print(json.dumps(report,ensure_ascii=False))
        return 0 if report["ok"] else 2
    except (ValueError,OSError,KeyError,csv.Error,ImportError) as error:
        print("错误："+str(error),file=sys.stderr)
        return 1


if __name__=="__main__":
    sys.exit(main())
