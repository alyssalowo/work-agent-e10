#!/usr/bin/env python3
"""Read-only packaging workorder planner and matching-column verifier."""

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from types import MappingProxyType
from decimal import Decimal, InvalidOperation
from pathlib import Path

SOURCE_FIELDS = ("需求单号", "产品品号", "特征码", "预计产量", "单号")
TARGET_FIELDS = ("单身来源单号", "销货明细_品号", "销货明细_特征码", "销货明细_业务数量", "工单单号")


def text(value):
    if type(value) is str:
        return value.replace("\r\n", "\n").replace("\r", "\n") if "\r" in value else value
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def identifier(value):
    return text(value).strip()


def quantity(value):
    raw = identifier(value)
    if not raw:
        raise ValueError("missing_quantity")
    if "," in raw:
        if not re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", raw):
            raise ValueError("invalid_quantity")
        raw = raw.replace(",", "")
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("invalid_quantity") from exc
    if not number.is_finite():
        raise ValueError("invalid_quantity")
    # Avoid Decimal.normalize(): it can round long values under the default context.
    fixed = format(number, "f")
    if "." in fixed:
        fixed = fixed.rstrip("0").rstrip(".")
    return "0" if number == 0 else fixed


def rectangle(rows):
    width = max((len(row) for row in rows), default=0)
    return [[text(v) for v in row] + [""] * (width - len(row)) for row in rows]


def digest(rows):
    normalized = [[text(v) for v in row] for row in rows]
    return _digest_text_rows(normalized)


def _digest_text_rows(rows):
    # Internal callers have already normalized every cell via rectangle().
    return hashlib.sha256(json.dumps(
        rows, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def cell_text(cell):
    value = cell.value
    # Preserve leading zeroes when Excel stores an identifier as a number.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if float(value).is_integer() and value >= 0 and re.fullmatch(r"0+", cell.number_format or ""):
            return str(int(value)).zfill(len(cell.number_format))
    return text(value)


def read_tables(path, sheet_names=None):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".tsv", ".csv"):
        if sheet_names:
            raise ValueError("TSV/CSV 不接受工作表参数")
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream, delimiter="\t" if suffix == ".tsv" else ","))
        return {path.stem: rectangle(rows)}
    if suffix not in (".xlsx", ".xlsm"):
        raise ValueError("仅支持 XLSX/XLSM/TSV/CSV；旧 XLS 须先另存为 XLSX")
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ValueError("缺少 openpyxl，请使用已捆绑该库的 Python") from exc
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        names = sheet_names or workbook.sheetnames
        missing = set(names) - set(workbook.sheetnames)
        if missing:
            raise ValueError("找不到工作表：" + ", ".join(sorted(missing)))
        result = {}
        for name in names:
            sheet = workbook[name]
            # Some online exports advertise A1 even when their XML has many rows.
            sheet.reset_dimensions()
            rows = [[cell_text(c) for c in row] for row in sheet.iter_rows()]
            width = max((len(row) for row in rows), default=0)
            result[name] = [row + [""] * (width-len(row)) for row in rows]
        return result
    finally:
        workbook.close()


def find_header(rows, required, search_rows):
    for index, row in enumerate(rows[:search_rows]):
        names = [identifier(v) for v in row]
        if all(name in names for name in required):
            duplicated = [name for name in required if names.count(name) != 1]
            if duplicated:
                raise ValueError("表头字段重复：" + ", ".join(duplicated))
            return index, {name: names.index(name) for name in required}
    return None


def make_key(demand, product, feature, amount, source=False):
    demand = identifier(demand)
    product = identifier(product)
    feature = identifier(feature)
    errors = []
    if source:
        parts = demand.rsplit("-", 2)
        if len(parts) != 3 or not all(parts):
            errors.append("missing_or_invalid_demand_suffix")
            demand = ""
        else:
            demand = parts[0]
    elif not demand:
        errors.append("missing_source_bill")
    if not product:
        errors.append("missing_product")
    try:
        amount = quantity(amount)
    except ValueError as exc:
        errors.append(str(exc))
        amount = identifier(amount)
    return (demand, product, feature, amount), errors


def source_index(tables, search_rows, explicitly_selected=False):
    index = defaultdict(set)
    counts = Counter()
    used_sheets = []
    for name, rows in tables.items():
        header = find_header(rows, SOURCE_FIELDS, search_rows)
        if not header:
            if explicitly_selected:
                raise ValueError(f"源工作表 {name} 缺少完整表头")
            continue
        used_sheets.append(name)
        head, columns = header
        key_columns = tuple(columns[n] for n in SOURCE_FIELDS[:4])
        order_column = columns["单号"]
        for row in rows[head + 1:]:
            if not any(identifier(v) for v in row):
                continue
            counts["records"] += 1
            key, errors = make_key(*(row[c] for c in key_columns), source=True)
            order = identifier(row[order_column])
            if not order or "\n" in order or "\t" in order:
                errors.append("invalid_workorder")
            if errors:
                counts["invalid_records"] += 1
                for reason in set(errors):
                    counts["invalid_" + reason] += 1
                continue
            counts["valid_records"] += 1
            if order in index[key]:
                counts["duplicate_candidate_records"] += 1
            index[key].add(order)
    if not used_sheets:
        raise ValueError("源文件没有包含完整匹配表头的工作表")
    return index, {"sheets": used_sheets, **dict(counts), "keys": len(index)}


def prepare_source_index(tables, search_rows, explicitly_selected=False):
    """Build an immutable source index reusable across all sheets in one batch."""
    index, stats = source_index(tables, search_rows, explicitly_selected)
    return (MappingProxyType({key: frozenset(orders) for key, orders in index.items()}),
            MappingProxyType({**stats, 'sheets': tuple(stats['sheets'])}))


def column_name(index):
    name = ""
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def stored_orders(value):
    return [part.strip() for part in text(value).split("\n") if part.strip()]


def build_plan(source_tables, target_rows, search_rows=20, row_offset=0, column_offset=0,
               selected_source=False, prepared_index=None):
    if prepared_index is None:
        source, source_stats = source_index(source_tables, search_rows, selected_source)
    else:
        source, source_stats = prepared_index
        source_stats = dict(source_stats)
        source_stats['sheets'] = list(source_stats['sheets'])
    target_rows = rectangle(target_rows)
    found = find_header(target_rows, TARGET_FIELDS, search_rows)
    if not found:
        raise ValueError("目标表缺少完整匹配表头")
    head, columns = found
    output_column = columns["工单单号"]
    column_letter = column_name(output_column + column_offset + 1)
    expected = [row[:] for row in target_rows]
    groups = defaultdict(list)
    candidate_cache = {}
    key_columns = tuple(columns[n] for n in TARGET_FIELDS[:4])
    details = []
    issues = []
    for offset in range(head + 1, len(target_rows)):
        row = target_rows[offset]
        if not any(identifier(v) for v in row):
            continue
        key, errors = make_key(*(row[c] for c in key_columns))
        candidates = []
        if not errors:
            if key not in candidate_cache:
                candidate_cache[key] = sorted(source.get(key, ()))
            candidates = candidate_cache[key][:]
        item = {
            "row": offset + row_offset + 1, "matrix_row": offset,
            "cell": f"{column_letter}{offset + row_offset + 1}",
            "key": list(key), "before": row[output_column],
            "after": row[output_column], "candidates": candidates,
            "errors": errors,
        }
        groups[key].append(item)
        details.append(item)

    def issue(items, reason, **extra):
        issues.append({"reason": reason, "rows": [i["row"] for i in items], **extra})
        for item in items:
            item["status"] = reason

    duplicate_groups = []
    for key, items in groups.items():
        candidates = items[0]["candidates"]
        if len(items) > 1:
            duplicate_groups.append({"key": list(key), "rows": [i["row"] for i in items],
                                     "candidates": candidates})
        if items[0]["errors"]:
            issue(items, "missing_or_invalid_fields", fields=items[0]["errors"])
            continue
        if not candidates:
            issue(items, "no_exact_match")
            continue
        candidate_set = set(candidates)
        conflicting = [item for item in items if set(stored_orders(item["before"])) - candidate_set]
        if conflicting:
            issue(items, "existing_value_conflict", conflicting_rows=[i["row"] for i in conflicting])
            continue
        if len(items) == 1:
            items[0]["after"] = "\n".join(candidates)
            items[0]["status"] = "single_candidate" if len(candidates) == 1 else "all_candidates_in_one_cell"
        elif len(candidates) < len(items):
            issue(items, "candidate_shortage", needed=len(items), available=len(candidates))
            continue
        else:
            # Keep existing valid distinct single assignments, including non-first candidates.
            retained = {}
            used = set()
            for item in items:
                orders = stored_orders(item["before"])
                if len(orders) == 1 and orders[0] in candidate_set and orders[0] not in used:
                    retained[item["row"]] = orders[0]
                    used.add(orders[0])
            remaining = iter(order for order in candidates if order not in used)
            for item in items:
                order = retained.get(item["row"])
                if order is None:
                    order = next(remaining)
                item["after"] = order
                item["status"] = "distinct_assignment"
                used.add(order)
            unused = [order for order in candidates if order not in used]
            if unused:
                issues.append({"reason": "unused_candidates", "rows": [i["row"] for i in items],
                               "candidates": unused})
        for item in items:
            expected[item["matrix_row"]][output_column] = item["after"]

    changes = [{k: item[k] for k in ("row", "cell", "key", "before", "after", "candidates", "status")}
               for item in details if item["before"] != item["after"]]
    first_row = head + row_offset + 2
    last_row = len(target_rows) + row_offset
    statuses = Counter(item["status"] for item in details)
    return {
        "schema_version": 1,
        "rules": {"source_demand_suffix_segments": 2,
                  "source_demand_suffix_separator": "-",
                  "single_row_multiple_candidates": "newline_all",
                  "duplicate_rows": "distinct_one_per_row",
                  "candidate_sort": "lexicographic", "shortage": "preserve_group"},
        "source_statistics": source_stats,
        "target": {"header_row": head + row_offset + 1, "row_offset": row_offset,
                   "column_offset": column_offset, "matrix_rows": len(target_rows),
                   "matrix_columns": len(target_rows[0]) if target_rows else 0,
                   "workorder_column_index": output_column, "workorder_column": column_letter,
                   "column_range": f"{column_letter}{first_row}:{column_letter}{last_row}"
                                   if last_row >= first_row else None},
        "statistics": {"business_rows": len(details), "changed_rows": len(changes),
                       "matched_rows": sum(len(items) for items in groups.values()
                                           if items[0]["candidates"]),
                       "filled_rows_after": sum(bool(identifier(i["after"])) for i in details),
                       "duplicate_groups": len(duplicate_groups), "statuses": dict(statuses)},
        "changes": changes, "duplicate_groups": duplicate_groups, "issues": issues,
        "rows": details, "before_hash": _digest_text_rows(target_rows), "after_hash": _digest_text_rows(expected),
        "before_matrix": target_rows, "expected_matrix": expected,
    }


def one_table(tables):
    if len(tables) != 1:
        raise ValueError("目标/快照含多个工作表，请明确指定 --target-sheet 或 --sheet")
    return next(iter(tables.items()))


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_column(plan, path):
    if not plan["target"]["column_range"]:
        raise ValueError("计划没有可写的数据范围")
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    col = plan["target"]["workorder_column_index"]
    start = plan["target"]["header_row"] - plan["target"]["row_offset"]
    with out.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\r\n")
        for row in plan["expected_matrix"][start:]:
            writer.writerow([row[col]])
    return plan["target"]["column_range"]


def differences(expected, actual, plan, limit=20):
    result = []
    count = 0
    for r in range(max(len(expected), len(actual))):
        left = expected[r] if r < len(expected) else []
        right = actual[r] if r < len(actual) else []
        if left == right:
            continue
        for c in range(max(len(left), len(right))):
            a = left[c] if c < len(left) else None
            b = right[c] if c < len(right) else None
            if a != b:
                count += 1
                if len(result) < limit:
                    result.append({"cell": f"{column_name(c + plan['target']['column_offset'] + 1)}"
                                          f"{r + plan['target']['row_offset'] + 1}",
                                   "expected": a, "actual": b})
    if expected != actual and count == 0:
        return 1, [{"reason": "matrix_shape_changed"}]
    return count, result


def matching_differences(expected, actual, plan, limit=20):
    """Keep layout guards, but compare only the four keys and workorder column."""
    if len(expected) != len(actual) or any(len(a) != len(b) for a, b in zip(expected, actual)):
        return differences(expected, actual, plan, limit)
    target = plan["target"]
    head = target["header_row"] - target["row_offset"] - 1
    header = [identifier(v) for v in plan["before_matrix"][head]]
    try:
        columns = [header.index(name) for name in TARGET_FIELDS[:4]]
    except ValueError as exc:
        raise ValueError("计划缺少完整匹配列") from exc
    columns.append(target["workorder_column_index"])
    scoped = [row[:] for row in expected]
    for r, row in enumerate(actual):
        for c in columns:
            scoped[r][c] = row[c]
    return differences(expected, scoped, plan, limit)


def same_input_output(inputs, outputs):
    input_paths = {Path(p).resolve() for p in inputs if p}
    output_paths = [Path(p).resolve() for p in outputs if p]
    if input_paths.intersection(output_paths) or len(output_paths) != len(set(output_paths)):
        raise ValueError("输出路径不能覆盖输入文件或其他输出")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan", help="只读匹配并生成方案")
    plan_parser.add_argument("--source", required=True)
    plan_parser.add_argument("--target", required=True)
    plan_parser.add_argument("--out", required=True)
    plan_parser.add_argument("--source-sheet", action="append")
    plan_parser.add_argument("--target-sheet")
    plan_parser.add_argument("--header-search-rows", type=int, default=20)
    plan_parser.add_argument("--target-row-offset", type=int, default=0)
    plan_parser.add_argument("--target-column-offset", type=int, default=0)
    for command in ("check-before", "verify"):
        sub = commands.add_parser(command, help="核对匹配列与工单列，差异时非零退出")
        sub.add_argument("--plan", required=True)
        sub.add_argument("--snapshot", required=True)
        sub.add_argument("--sheet")
        sub.add_argument("--out", help="可选的核对报告 JSON")
    export = commands.add_parser("column-tsv", help="生成保留原值的单列标准 TSV")
    export.add_argument("--plan", required=True)
    export.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            if args.header_search_rows < 1 or min(args.target_row_offset, args.target_column_offset) < 0:
                raise ValueError("表头搜索行数须为正，范围偏移须为非负")
            same_input_output([args.source, args.target], [args.out])
            source = read_tables(args.source, args.source_sheet)
            name, target = one_table(read_tables(args.target, [args.target_sheet] if args.target_sheet else None))
            plan = build_plan(source, target, args.header_search_rows, args.target_row_offset,
                              args.target_column_offset, bool(args.source_sheet))
            plan["source_file"] = str(Path(args.source).resolve())
            plan["target_file"] = str(Path(args.target).resolve())
            plan["target"]["sheet"] = name
            save_json(args.out, plan)
            print(json.dumps({"plan": str(Path(args.out).resolve()), **plan["statistics"],
                              "issue_groups": len(plan["issues"])}, ensure_ascii=False))
            return 0
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8-sig"))
        if plan.get("schema_version") != 1:
            raise ValueError("不支持此计划版本")
        if args.command == "column-tsv":
            same_input_output([args.plan, plan.get("source_file"), plan.get("target_file")], [args.out])
            print(json.dumps({"range": write_column(plan, args.out), "file": args.out}, ensure_ascii=False))
            return 0
        same_input_output([args.plan, args.snapshot, plan.get("source_file"), plan.get("target_file")], [args.out])
        _, actual = one_table(read_tables(args.snapshot, [args.sheet] if args.sheet else None))
        expected = plan["before_matrix"] if args.command == "check-before" else plan["expected_matrix"]
        count, sample = matching_differences(expected, actual, plan)
        report = {"ok": count == 0, "command": args.command, "difference_count": count,
                  "expected_hash": digest(expected), "actual_hash": digest(actual), "differences": sample}
        if args.out:
            save_json(args.out, report)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 2
    except (ValueError, OSError, csv.Error, KeyError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
