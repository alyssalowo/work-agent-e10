#!/usr/bin/env python3
"""Guarded manual-19 scheduling over the caller's existing MCP session.

No native calls, browser control, automatic login, retry, or cached BOM reuse.
"""
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import uuid

ZERO = '00000000-0000-0000-0000-000000000000'
FIELDS = ('单号', '销货明细_品号', '销货明细_品名', '销货明细_特征码', '销货明细_业务数量', '包装工单', '排产日期')
ARGUMENTS = {'item_code', 'feature_code', 'plant_id', 'quantity', 'doc_date', 'start_date', 'complete_date', 'reference'}
HEADER = ('MO_ID', 'DOC_ID', 'ITEM_ID', 'ITEM_FEATURE_ID', 'PLAN_QTY', 'BUSINESS_UNIT_ID',
          'DOC_DATE', 'TRANSACTION_DATE', 'PLAN_START_DATE', 'PLAN_COMPLETE_DATE', 'BOM_DATE',
          'BOM_VERSION_TIMES', 'Owner_Emp', 'Owner_Dept', 'Owner_Org', 'SOURCE_ID', 'ADMIN_UNIT_ID',
          'WAREHOUSE_ID', 'REMARK', 'ITEM_ROUTING_CONTROL', 'ITEM_ROUTING_ID', 'ROUTING_CONTROL')
AUDIT_FIELDS = {'ApproveBy', 'ApproveDate', 'ApproveStatus', 'CreateBy', 'CreateDate',
                'LastModifiedBy', 'LastModifiedDate', 'ModifiedBy', 'ModifiedDate', 'Version'}
BOM_FIELDS = ('ITEM_ID', 'ITEM_SPECIFICATION', 'UNIT_ID', 'ITEM_FEATURE_ID', 'UP_LEVEL_ITEM_ID',
              'UP_LEVEL_ITEM_FEATURE_ID', 'REPLACED_ITEM_ID', 'REPLACED_ITEM_FEATURE_ID', 'ITEM_GROUP',
              'REPLACE_GROUP', 'REQUIRED_QTY', 'REQU_INCLUDE_MULT_QTY', 'LEAST_REQUIRED_QTY',
              'OPERATION_ID', 'ITEM_TYPE', 'REMARK', 'ISSUE_OVERRUN_RATE', 'ISSUE_SHORTAGE_RATE',
              'ISSUE_MATERIAL_PERIOD', 'COMPONENT_LOCATION', 'REPLACED_QTY')


def need(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def local(base, value):
    base = Path(base).resolve()
    path = (base / value).resolve()
    need(path != base and path.is_relative_to(base), '路径须位于本轮工作目录')
    return path


def number(value):
    need(not isinstance(value, bool), '数量不能为布尔值')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('数量不是精确数值') from exc
    need(result.is_finite() and result > 0, '数量须为有限正数')
    return result


def quantity_text(value):
    return format(number(value).normalize(), 'f')


def gid(value):
    return str(uuid.UUID(str(value)))


def nonzero(value):
    return gid(value) != ZERO


def document_key(url):
    parsed = urlsplit(url)
    need(parsed.scheme == 'https' and parsed.hostname in ('alidocs.dingtalk.com', 'docs.dingtalk.com')
         and not parsed.username and not parsed.password and parsed.port in (None, 443),
         '目标须为本次钉钉包装文档')
    match = re.fullmatch(r'/i/nodes/([A-Za-z0-9_-]+)', parsed.path)
    need(match is not None, '目标文档标识不完整')
    return match[1]


def snapshot_rows(scope, snapshot):
    if isinstance(snapshot, (str, Path)):
        snapshot = read(snapshot)
    need(snapshot.get('ok') is True and snapshot.get('schema_version') == 1 and snapshot.get('phase') == 'collect',
         '需要本轮完整成功采集')
    need(document_key(scope['target_url']) == document_key(snapshot['target_url']), '目标文档不符')
    native = snapshot.get('transport') == 'dingtalk_mcp'
    sheet_transport = snapshot.get('transport') == 'dingtalk_sheet'
    if native:
        need(snapshot.get('complete') is True and snapshot.get('base_id') == document_key(scope['target_url']),
             'MCP来源分页不完整或Base身份不符')
    elif sheet_transport:
        need(snapshot.get('complete') is True and snapshot.get('node_id') == document_key(scope['target_url']),
             '普通表来源文档不符或采集不完整')
    else:
        need(False, '不支持的来源快照传输类型')
    found = {}
    for sheet in snapshot['sheets']:
        need(sheet['name'] not in found and re.fullmatch(r'A1:[A-Z]+[1-9]\d*', sheet['range']), '工作表身份或范围不完整')
        rows = list(csv.reader(io.StringIO(sheet['tsv']), delimiter='\t'))
        if native:
            meta = sheet.get('mcp', {})
            records, fields = meta.get('record_ids', []), meta.get('fields', [])
            ids = [f['field_id'] for f in fields]
            names = [f['name'] for f in fields]
            need(sheet.get('scope_complete') is True and sheet.get('next_cursor', 'missing') in (None, '')
                 and meta.get('base_id') == snapshot['base_id'] and meta.get('table_id') == sheet.get('sheet_id')
                 and bool(meta.get('table_id')) and all(records) and len(set(records)) == len(records)
                 and bool(ids) and all(ids) and len(set(ids)) == len(ids) and len(set(names)) == len(names)
                 and rows and rows[0] == names and len(rows) == len(records) + 1
                 and all(len(r) == len(fields) for r in rows), 'MCP来源记录、字段或完整性映射无效')
        if sheet_transport:
            meta = sheet.get('sheet_mcp', {})
            end = re.fullmatch(r'A1:([A-Z]+)([1-9]\d*)', sheet['range'])
            def colnum(label):
                n = 0
                for ch in label: n = n * 26 + ord(ch) - 64
                return n
            def collabel(n):
                out = ''
                while n:
                    n, rem = divmod(n - 1, 26); out = chr(65 + rem) + out
                return out
            need('mcp' not in sheet and set(meta) == {'node_id','sheet_id','row_indices','col_indices','has_more'}
                 and sheet.get('scope_complete') is True and end is not None and sheet.get('sheet_id')
                 and meta.get('node_id') == snapshot['node_id'] and meta.get('sheet_id') == sheet['sheet_id']
                 and meta.get('has_more') is False and meta.get('row_indices') == list(range(1, int(end[2]) + 1))
                 and meta.get('col_indices') == [collabel(i) for i in range(1, colnum(end[1])+1)]
                 and len(rows) == int(end[2]) and rows and all(len(r) == colnum(end[1]) for r in rows),
                 '普通表范围、行列映射或完整性无效')
        headers = [(i, {f: r.index(f) for f in FIELDS}) for i, r in enumerate(rows[:20])
                   if all(r.count(f) == 1 for f in FIELDS)]
        need(len(headers) == 1, '19来源表头不唯一或缺失')
        found[sheet['name']] = (rows, *headers[0])
    return found


def compare_source_snapshots(scope, before, after):
    """Protect all 19-source rows by A1 position and seven business fields."""
    if isinstance(before, (str, Path)): before = read(before)
    if isinstance(after, (str, Path)): after = read(after)
    old, new = snapshot_rows(scope, before), snapshot_rows(scope, after)
    need(list(old) == list(new), '写前快照工作表集合或顺序变化')
    for name in old:
        a, b = old[name], new[name]
        sa = next(x for x in before['sheets'] if x['name'] == name)
        sb = next(x for x in after['sheets'] if x['name'] == name)
        need(sa.get('sheet_id') == sb.get('sheet_id') and sa['range'] == sb['range'], '写前快照sheetID、完整范围或顺序变化')
        rows_a, head_a, cols_a = a
        rows_b, head_b, cols_b = b
        need(head_a == head_b and cols_a == cols_b and len(rows_a) == len(rows_b), '写前快照表格位置或尺寸变化')
        for index, row in enumerate(rows_a):
            if index <= head_a:
                continue
            other = rows_b[index]
            need(len(row) > max(cols_a.values()) and len(other) > max(cols_b.values()), '写前快照来源行列不完整')
            was_19, is_19 = row[cols_a['单号']].startswith('19'), other[cols_b['单号']].startswith('19')
            if was_19 or is_19:
                need(was_19 and is_19 and
                     all(row[cols_a[f]] == other[cols_b[f]] for f in FIELDS),
                     '写前快照19来源受保护字段变化或行移动')


def source_check_basis(prepared_snapshot, source_check):
    """Describe validated source evidence without claiming a remote refresh."""
    before = Path(prepared_snapshot).resolve()
    checked = Path(source_check).resolve()
    return {
        'kind': 'same_prepared_snapshot_reused' if before == checked else 'separate_snapshot_revalidated',
        'path': str(checked),
        'sha256': digest(checked),
        'online_refresh_claimed': False,
    }


def validate_source_check(scope, prepared_snapshot, source_check, expected_sha256=None):
    """Validate reused or separate source evidence against the complete 19-row scope."""
    before_path, check_path = Path(prepared_snapshot).resolve(), Path(source_check).resolve()
    need(before_path.is_file() and check_path.is_file(), '来源证据文件不存在')
    if expected_sha256 is not None:
        need(digest(before_path) == expected_sha256, '原准备来源快照哈希已变化')
    before, after = read(before_path), read(check_path)
    if scope.get('specs'):
        check_source(scope, after)
    else:
        snapshot_rows(scope, after)
    compare_source_snapshots(scope, before, after)
    return source_check_basis(before_path, check_path)


def check_source(scope, snapshot):
    """Check every requested row and stable duplicate ordinal; never change sheet data."""
    tables = snapshot_rows(scope, snapshot)
    overrides = scope.get('feature_overrides', {})
    keys, references = set(), set()
    specs = scope['specs']
    need(bool(specs), '没有明确的19来源开单范围')
    excluded = {(v['sheet'], v['row']) for v in scope.get('excluded_rows', [])}
    occurrences = {}
    for sheet, (rows, head, cols) in tables.items():
        count = {}
        for i, row in enumerate(rows[head + 1:], head + 2):
            if len(row) <= max(cols.values()) or not row[cols['单号']].startswith('19'):
                continue
            item, feature, qty = (row[cols[f]].strip() for f in FIELDS[1:2] + FIELDS[3:5])
            override = overrides.get(f'{sheet}:{i}')
            if override:
                need(override.get('basis') and override['item_code'] == item, '特征覆盖缺少用户依据或品号不符')
                need(feature in ('', override['feature_code']), '源特征码与用户覆盖冲突')
                feature = override['feature_code']
            try:
                key = (row[cols['单号']], item, feature, quantity_text(qty))
            except ValueError:
                continue  # Invalid unrelated rows cannot silently become scope entries below.
            count[key] = count.get(key, 0) + 1
            occurrences[(sheet, i)] = (key, count[key])
    for spec in specs:
        sheet, row_index, args = spec['sheet'], spec['row'], spec['arguments']
        key = (sheet, row_index)
        need(type(row_index) is int and key not in keys and key not in excluded, '行号重复、无效或已排除')
        keys.add(key)
        need(set(args) == ARGUMENTS and args['item_code'].startswith('1-'), '仅支持本次明确的1-成品参数')
        need(sheet in tables, '指定工作表未完整采集')
        rows, _, cols = tables[sheet]
        need(1 <= row_index <= len(rows), '来源行已移动或缺失')
        if snapshot.get('transport') == 'dingtalk_mcp':
            collected = next(s for s in snapshot['sheets'] if s['name'] == sheet)
            record_ids = collected['mcp']['record_ids']
            need(2 <= row_index <= len(record_ids) + 1
                 and spec.get('record_id') == record_ids[row_index - 2], '来源行recordId已变化或未记录')
        elif snapshot.get('transport') == 'dingtalk_sheet':
            collected = next(s for s in snapshot['sheets'] if s['name'] == sheet)
            need(spec.get('sheet_id') == collected['sheet_id'], '普通表来源sheetID已变化或未记录')
            need('record_id' not in spec, '普通表范围不能伪造recordId')
        raw = rows[row_index - 1]
        need(len(raw) > max(cols.values()) and raw[cols['包装工单']].strip() == '', '已有工单不能重开')
        need('source_plan_date' in spec and raw[cols['排产日期']] == spec['source_plan_date'], '来源排产日期已变化或未记录')
        need(key in occurrences, '指定行不再是有效19来源')
        actual, occurrence = occurrences[key]
        expected = (spec['source_no'], args['item_code'], args['feature_code'], quantity_text(args['quantity']))
        need(actual == expected and actual[0].startswith('19'), '来源单号、品号、特征码或数量已变化')
        rules = scope['selection_rules']
        name = raw[cols['销货明细_品名']]
        need(not (args['item_code'].startswith('1-V') and any(v.casefold() in name.casefold() for v in rules['excluded_1_v_name_keywords'])) and
             not (args['item_code'].startswith('1-H') and any(v.casefold() in name.casefold() for v in rules['excluded_1_h_name_keywords'])), '指定成品属于本轮排除范围')
        need(gid(args['plant_id']) == args['plant_id'] and nonzero(args['plant_id']), '工厂ID无效')
        for field in ('doc_date', 'start_date', 'complete_date'):
            need(datetime.strptime(args[field], '%Y-%m-%d').strftime('%Y-%m-%d') == args[field], '日期格式错误')
        need(args['complete_date'] >= args['start_date'], '完工日期早于开工日期')
        q = number(args['quantity'])
        need(Decimal(str(float(q))) == q, '数量不能无损传入现行MCP数值参数')
        reference = f'包装计划:{document_key(scope["target_url"])}:{sheet}:{actual[0]}:{actual[1]}:{actual[2]}:{actual[3]}:{occurrence}'
        need(args['reference'] == reference and len(reference) <= 200 and reference not in references, '稳定来源序号或reference不符')
        references.add(reference)
    return len(keys)


def preview_key(spec):
    values = {k: v for k, v in spec['arguments'].items() if k != 'reference'}
    values['quantity'] = quantity_text(values['quantity'])
    return json.dumps(values, sort_keys=True, ensure_ascii=False)


def expected_master(preview, args, plant_name):
    need(preview.get('ok') is True and preview.get('saved') is False, '主资料/BOM预览失败')
    for key in ('item_code', 'feature_code'):
        need(preview[key] == args[key], '预览产品或特征不符')
    need(number(preview['quantity']) == number(args['quantity']), '预览数量不符')
    need(preview['plant']['PLANT_ID'] == args['plant_id'] and preview['plant']['PLANT_NAME'] == plant_name, '预览工厂不符')
    item, ip, person = preview['item'], preview['item_plant'], preview['current_employee']
    need(item['UNIT_MODE'] == 'S' and ip['ITEM_ROUTING_CONTROL'] == '0', '双单位或工艺管理需要专用流程')
    center = ip['WORK_CENTER_ID']
    if not nonzero(center):
        center_data = preview.get('work_center', {})
        need(preview.get('work_center_fallback') is True and center_data.get('WORK_CENTER_NAME') == '生产车间', '缺失工作中心未唯一解析为生产车间')
        center = center_data['WORK_CENTER_ID']
    department = ip.get('PRODUCTION_ADMIN_UNIT_ID')
    if not nonzero(department):
        department_data = preview.get('production_department', {})
        need(preview.get('production_department_fallback') is True and
             department_data.get('ADMIN_UNIT_NAME') == '生产部' and
             department_data.get('ApproveStatus') == 'Y', '缺失生产部门未唯一解析为有效生产部')
        department = department_data.get('ADMIN_UNIT_ID')
    else:
        need(preview.get('production_department_fallback') is not True, '已有生产部门不应被默认值替换')
        if preview.get('production_department'):
            need(preview['production_department'].get('ADMIN_UNIT_ID') == department, '生产部门证据与原主资料不符')
    need(all(nonzero(v) for v in (center, department, person['EMPLOYEE_ID'], person['DEPARTMENT_ID'], item['STOCK_UNIT_ID'])), '必需主资料缺失')
    need(bool(preview['bom_version']) and bool(preview['bom_rows']), '当前BOM不完整')
    return {'item': item, 'item_plant': ip, 'person': person, 'work_center_id': center, 'production_department_id': department,
            'feature_id': preview['feature']['ITEM_FEATURE_ID'] if preview.get('feature') else ZERO}


def check_prepared(result, spec, preview, doc_id, plant_name):
    args = spec['arguments']; master = expected_master(preview, args, plant_name)
    need(result.get('ok') is True and result.get('state') == 'prepared' and result.get('saved') is False and
         result.get('module') == 'MO' and result.get('action') == 'create', '未取得有效工单内存草稿')
    need(re.fullmatch(r'[0-9a-f]{32}', result['operation_id']) is not None, '操作ID无效')
    summary, draft = result['summary'], result['summary']['draft']
    for key in ('item_code', 'feature_code', 'reference'):
        need(summary[key] == args[key], '草稿行级参数不符')
    need(number(summary['quantity']) == number(args['quantity']), '草稿数量不符')
    need(summary['plant_name'] == plant_name and draft['Owner_Org'] == {'RTK': 'PLANT', 'ROid': args['plant_id']}, '草稿工厂不符')
    expected = {'DOC_ID': doc_id, 'ITEM_ID': master['item']['ITEM_ID'], 'ITEM_FEATURE_ID': master['feature_id'],
                'BUSINESS_UNIT_ID': master['item']['STOCK_UNIT_ID'], 'ADMIN_UNIT_ID': master['production_department_id'],
                'Owner_Emp': master['person']['EMPLOYEE_ID'], 'Owner_Dept': master['person']['DEPARTMENT_ID'],
                'SOURCE_ID': {'RTK': 'WORK_CENTER', 'ROid': master['work_center_id']},
                'BOM_VERSION_TIMES': preview['bom_version'], 'ITEM_ROUTING_CONTROL': '0', 'ITEM_ROUTING_ID': ZERO,
                'STATUS': '1', 'REMARK': args['reference'], 'WAREHOUSE_ID': master['item_plant']['INBOUND_WAREHOUSE_ID']}
    for key, value in expected.items():
        need(draft[key] == value, '草稿字段不符：' + key)
    need(draft['ApproveStatus'] in ('', 'N') and not draft['DOC_NO'] and nonzero(draft['MO_ID']) and number(draft['PLAN_QTY']) == number(args['quantity']), '新单身份、状态或数量异常')
    for key, arg in (('DOC_DATE','doc_date'),('TRANSACTION_DATE','doc_date'),('BOM_DATE','doc_date'),('PLAN_START_DATE','start_date'),('PLAN_COMPLETE_DATE','complete_date')):
        need(draft[key][:10] == args[arg], '草稿日期不符：' + key)
    allocations = {a['index']: a for a in preview['bom_parameters']}
    need(len(allocations) == len(preview['bom_parameters']), 'BOM分配索引重复')
    bom = [(i, row) for i, row in enumerate(preview['bom_rows']) if int(row['LEV']) != 0]
    need(len(draft['MO_D']) == len(bom) == summary['component_count'] == len(summary['components']) > 0, 'BOM子件数量不完整')
    for line, evidence, (index, row) in zip(draft['MO_D'], summary['components'], bom):
        need(index in allocations and nonzero(allocations[index]['warehouse_id']), 'BOM缺少实际仓库')
        for key in BOM_FIELDS:
            need(line[key] == row[key], 'BOM字段不符：' + key)
        need(line['ITEM_DESCRIPTION'] == row['ITEM_NAME'] and line['REPLACE_ITEM'] == row['REPLACE_TYPE'], 'BOM品名或替代类型不符')
        need(line['WAREHOUSE_ID'] == allocations[index]['warehouse_id'] and line['BIN_ID'] == (allocations[index]['bin_id'] or ZERO), '子件仓库/储位不符')
        need(line['PLAN_ISSUE_DATE'][:10] == args['start_date'], '子件领料日期不符')
        need(evidence == {'item_code': row['ITEM_CODE'], 'quantity': line['REQUIRED_QTY'], 'warehouse_id': line['WAREHOUSE_ID']}, '子件证据不完整')
    need(len(draft['MO_PRODUCT']) == 1, '产出行不唯一')
    product = draft['MO_PRODUCT'][0]
    for key in ('ITEM_ID','ITEM_FEATURE_ID','ITEM_DESCRIPTION','ITEM_SPECIFICATION','PLAN_QTY','WAREHOUSE_ID'):
        need(product[key] == draft[key], '产出字段不符：' + key)
    need(product['UNIT_ID'] == draft['BUSINESS_UNIT_ID'] and product['PRODUCT_TYPE'] == '1' and product['PER_QTY'] == 1, '产出单位或类型不符')
    return draft


def same_document(actual, expected, approval):
    for key in HEADER:
        need(actual[key] == expected[key], '保存/审核业务字段变化：' + key)
    need(re.fullmatch(r'5101-[A-Za-z0-9]+', actual['DOC_NO']) is not None and actual['ApproveStatus'] == approval, '正式单号或审核状态不符')
    for collection in ('MO_D', 'MO_PRODUCT'):
        need(len(actual[collection]) == len(expected[collection]), '正式子件或产出行数变化')
        for a, b in zip(actual[collection], expected[collection]):
            need({k:v for k,v in a.items() if k not in AUDIT_FIELDS} == {k:v for k,v in b.items() if k not in AUDIT_FIELDS}, '正式子件/产出字段变化')


class ManualRun:
    def __init__(self, call, base, directory, session_id, emit):
        self.call, self.base, self.directory, self.session_id, self.emit = call, base, directory, session_id, emit

    async def invoke(self, name, arguments, relative):
        path = self.directory / relative
        need(not path.exists(), '回执已存在，禁止覆盖或重放：' + relative)
        started = datetime.now(timezone.utc).isoformat(); tick = time.perf_counter(); answer = None
        try:
            answer = await self.call(name, **arguments)
            save(path, answer)
            return answer
        finally:
            metric = {'tool':name, 'started_utc':started, 'elapsed_seconds':round(time.perf_counter()-tick, 6),
                      'response_path':str(path), 'ok':answer.get('ok') if isinstance(answer,dict) else False}
            if isinstance(answer,dict):
                metric.update({k:answer[k] for k in ('state','operation_id') if k in answer})
            with (self.directory/'calls.jsonl').open('a', encoding='utf-8') as output:
                output.write(json.dumps(metric, ensure_ascii=False)+'\n')

    async def prepare(self, scope_path):
        need(not self.directory.exists(), '阶段目录已存在；先核对原记录，不能覆盖')
        self.directory.mkdir(parents=True)
        scope = read(scope_path); source_path = local(self.base, scope['source_snapshot'])
        check_source(scope, read(source_path))
        doc = await self.invoke('e10_lookup_master', {'kind':'DOC','code':'5101'}, 'document-type.json')
        need(doc.get('ok') and doc.get('unique') is True and doc.get('has_next_page') is False and len(doc['rows']) == 1 and doc['rows'][0]['CODE'] == '5101', '包装工单类型未唯一解析')
        previews = {}; packets = []; ids = set(); oids = set(); child_ids = set()
        for spec in scope['specs']:
            key = preview_key(spec)
            if key not in previews:
                args = {k:v for k,v in spec['arguments'].items() if k != 'reference'}
                args['quantity'] = float(number(args['quantity']))
                result = await self.invoke('e10_preview_manual_workorder', args, f'preview-{len(previews):03}.json')
                expected_master(result, spec['arguments'], scope['plant_name'])
                previews[key] = result
        # Previews for every unique parameter group must pass before any per-row preparation.
        first_prepare_utc = datetime.now(timezone.utc).isoformat()
        for index, spec in enumerate(scope['specs']):
            args = {**spec['arguments'], 'quantity':float(number(spec['arguments']['quantity']))}
            name = f'prepare-{index:03}.json'
            result = await self.invoke('e10_prepare_manual_workorder', args, name)
            draft = check_prepared(result, spec, previews[preview_key(spec)], doc['rows'][0]['ID'], scope['plant_name'])
            need(result['operation_id'] not in ids and draft['MO_ID'] not in oids, '多行复用了操作或工单ID')
            new_ids = [r['MO_D_ID'] for r in draft['MO_D']] + [r['MO_PRODUCT_ID'] for r in draft['MO_PRODUCT']]
            need(len(new_ids) == len(set(new_ids)) and not child_ids.intersection(new_ids), '多行复用了子件或产出ID')
            ids.add(result['operation_id']); oids.add(draft['MO_ID']); child_ids.update(new_ids)
            packets.append({'spec':spec, 'operation_id':result['operation_id'], 'mo_id':draft['MO_ID'], 'receipt':name, 'sha256':digest(self.directory/name)})
        files = [p for p in self.directory.glob('*.json')]
        prepared = {'ok':True, 'schema_version':1, 'session_id':self.session_id, 'prepared_utc':datetime.now(timezone.utc).isoformat(),
                    'first_prepare_utc':first_prepare_utc,
                    'scope':str(scope_path), 'scope_sha256':digest(scope_path), 'source_snapshot':str(source_path),
                    'source_sha256':digest(source_path), 'unique_groups':len(previews), 'count':len(packets), 'packets':packets,
                    'evidence_sha256':{p.name:digest(p) for p in files}, 'saved':False}
        save(self.directory/'prepared.json', prepared)
        return {'ok':True, 'phase':'manual_prepare', 'prepared_count':len(packets), 'unique_groups':len(previews),
                'prepared_file':str(self.directory/'prepared.json'), 'prepared_sha256':digest(self.directory/'prepared.json'), 'saved':False}

    async def commit(self, operation_id, relative, checkpoint, phase, identity):
        need(re.fullmatch(r'[0-9a-f]{32}', operation_id) is not None, '操作ID无效')
        marker = local(self.base, 'commit-started-'+operation_id+'.json')
        with marker.open('x', encoding='utf-8') as output:
            json.dump({'operation_id':operation_id, 'phase':phase, 'identity':identity, 'receipt':str(self.directory/relative)}, output, ensure_ascii=False)
        checkpoint['in_flight'] = {'operation_id':operation_id,'phase':phase,**identity}
        save(self.directory/'checkpoint.json', checkpoint)
        answer = await self.invoke('e10_commit_operation', {'operation_id':operation_id}, relative)
        need(answer.get('ok') and answer.get('state') == 'completed' and answer.get('operation_id') == operation_id, '提交未明确完成，禁止继续依赖写入')
        return answer

    async def create(self, approved_sha256, source_check):
        prepared_path = self.directory/'prepared.json'
        need(digest(prepared_path) == approved_sha256, '未核对的完整准备计划或计划已变化')
        prepared = read(prepared_path)
        need(prepared['ok'] and prepared['session_id'] == self.session_id, '必须使用原MCP会话，不能重放内存计划')
        age = (datetime.now(timezone.utc)-datetime.fromisoformat(prepared['first_prepare_utc'])).total_seconds()
        need(0 <= age < 25*60, '准备时间过长；先丢弃并重新核对未保存对象，不提交过期计划')
        need(not (self.directory/'checkpoint.json').exists(), '已有执行检查点，先核实实际结果，不能自动重放')
        scope_path = Path(prepared['scope']); scope = read(scope_path)
        need(digest(scope_path) == prepared['scope_sha256'] and digest(Path(prepared['source_snapshot'])) == prepared['source_sha256'], '原范围或快照已被改写')
        for name, fingerprint in prepared['evidence_sha256'].items():
            need(digest(self.directory/name) == fingerprint, '准备证据已变化')
        check_basis = validate_source_check(scope, prepared['source_snapshot'], source_check,
                                            prepared['source_sha256'])
        packets = prepared['packets']; need([p['spec'] for p in packets] == scope['specs'], '准备范围不完整')
        for packet in packets:
            need(digest(self.directory/packet['receipt']) == packet['sha256'], '草稿证据已变化')
        status = await self.invoke('e10_connection_status', {}, 'status-before-create-'+uuid.uuid4().hex+'.json')
        need(status.get('ok') and status.get('authenticated') is True, '认证已失效，未开始执行；显式恢复后先核实草稿会话')
        checkpoint = {'ok':False, 'stage':'creating', 'prepared_sha256':approved_sha256, 'created':[], 'confirmed':[], 'bindings':[]}
        save(self.directory/'checkpoint.json', checkpoint)
        used_numbers = set()
        for i, packet in enumerate(packets):
            spec = packet['spec']; identity = {'sheet':spec['sheet'],'row':spec['row']}
            draft = read(self.directory/packet['receipt'])['summary']['draft']
            answer = await self.commit(packet['operation_id'], f'create-{i:03}.json', checkpoint, 'create', identity)
            actual = answer['result']; same_document(actual, draft, 'N')
            need(actual['DOC_NO'] not in used_numbers, '重复行产生相同正式工单号')
            used_numbers.add(actual['DOC_NO']); checkpoint['created'].append({'index':i, 'document_no':actual['DOC_NO'], 'mo_id':actual['MO_ID']})
            checkpoint['bindings'].append({'sheet':spec['sheet'],'row':spec['row'],'source_no':spec['source_no'],
                                           'item_code':spec['arguments']['item_code'],'feature_code':spec['arguments']['feature_code'],
                                           'quantity':quantity_text(spec['arguments']['quantity']),'reference':spec['arguments']['reference'],
                                           'workorder_no':actual['DOC_NO'],'mo_id':actual['MO_ID'],'approval_status':'N',
                                           'receipt_directory':str(self.directory)})
            checkpoint.pop('in_flight'); save(self.directory/'checkpoint.json', checkpoint)
        checkpoint['ok'] = True; checkpoint['stage'] = 'created'
        save(self.directory/'checkpoint.json', checkpoint)
        creation = {'ok':True,'complete':True,'created_count':len(packets),'approved_count':0,
                    'documents':[{'ID':v['mo_id'],'DOC_NO':v['document_no']} for v in checkpoint['created']],
                    'bindings':checkpoint['bindings'],'prepared_sha256':approved_sha256,
                    'scope_sha256':prepared['scope_sha256'],'source_check':str(source_check),
                    'source_check_sha256':digest(source_check),'session_id':self.session_id,
                    'source_check_basis':check_basis,
                    'create_receipts_sha256':{f'create-{i:03}.json':digest(self.directory/f'create-{i:03}.json') for i in range(len(packets))},
                    'picking_gate_requires_all_other_rows':True}
        save(self.directory/'creation-completion.json', creation)
        return {'ok':True,'phase':'manual_create','created':len(packets),'approved':0,
                'creation_completion':str(self.directory/'creation-completion.json')}

    def complete_after_approval(self, approved_by_no, excluded_by_no=None, continuation_id=None):
        """Validate complete documents supplied by the unified approver; never call MCP.

        The caller must obtain each document from an actual commit/get-document
        receipt after both creation branches finish. This method cannot approve
        or repair missing evidence and publishes completion only after every row
        passes the original full business-document comparison. A resumed run
        may provide separately evidenced N exclusions; those rows remain in a
        derived excluded_bindings list and never enter the Y bindings.
        """
        need(isinstance(approved_by_no, dict), '须提供统一审核器取得的完整已审单据映射')
        excluded_by_no = excluded_by_no or {}
        derived = continuation_id is not None
        need(isinstance(excluded_by_no, dict) and (not derived or re.fullmatch(r'[a-f0-9]{32}', continuation_id)),
             '续办排除清单或ID不符')
        if not derived:
            need(not (self.directory/'completion.json').exists(), '已存在最终完成记录，禁止覆盖或重放')
        creation = read(self.directory/'creation-completion.json')
        checkpoint = read(self.directory/'checkpoint.json')
        need(creation.get('ok') is True and creation.get('complete') is True and creation.get('approved_count') == 0,
             '手工开单阶段未完整完成')
        need(checkpoint.get('ok') is True and checkpoint.get('stage') == 'created' and
             checkpoint.get('confirmed') == [] and 'in_flight' not in checkpoint, '开单检查点未完整完成或结果未知')
        prepared_path = self.directory/'prepared.json'; prepared = read(prepared_path)
        need(digest(prepared_path) == creation['prepared_sha256'] == checkpoint['prepared_sha256'], '完整准备计划已变化')
        scope_path = Path(prepared['scope']); scope = read(scope_path)
        need(digest(scope_path) == prepared['scope_sha256'] == creation['scope_sha256'], '原开单范围已变化')
        need(digest(Path(prepared['source_snapshot'])) == prepared['source_sha256'] and
             digest(Path(creation['source_check'])) == creation['source_check_sha256'], '开单来源证据已变化')
        for name, fingerprint in prepared['evidence_sha256'].items():
            need(digest(self.directory/name) == fingerprint, '准备证据已变化')
        packets = prepared['packets']
        need(bool(packets) and [p['spec'] for p in packets] == scope['specs'] and
             creation['created_count'] == len(packets) == len(checkpoint['created']), '开单范围不完整')
        expected_receipts = {f'create-{i:03}.json' for i in range(len(packets))}
        need(set(creation['create_receipts_sha256']) == expected_receipts, '保存回执清单不完整')
        bindings = []; source_bindings = []; excluded_bindings = []; documents = []; approvals = {}; created = []
        for i, packet in enumerate(packets):
            name = f'create-{i:03}.json'; path = self.directory/name
            need(digest(path) == creation['create_receipts_sha256'][name], '保存回执已变化')
            need(digest(self.directory/packet['receipt']) == packet['sha256'], '草稿证据已变化')
            receipt = read(path)
            need(receipt.get('ok') and receipt.get('state') == 'completed' and
                 receipt.get('operation_id') == packet['operation_id'], '保存回执未明确完成')
            saved = receipt['result']; draft = read(self.directory/packet['receipt'])['summary']['draft']
            same_document(saved, draft, 'N')
            no = saved['DOC_NO']
            spec = packet['spec']
            binding = {'sheet':spec['sheet'],'row':spec['row'],'source_no':spec['source_no'],
                       'item_code':spec['arguments']['item_code'],'feature_code':spec['arguments']['feature_code'],
                       'quantity':quantity_text(spec['arguments']['quantity']),'reference':spec['arguments']['reference'],
                       'workorder_no':no,'mo_id':saved['MO_ID'],'approval_status':'N','receipt_directory':str(self.directory)}
            source_bindings.append(binding)
            if no in excluded_by_no:
                need(derived and no not in approved_by_no, '排除项不能同时出现在已审清单')
                exclusion = excluded_by_no[no]
                need(isinstance(exclusion, dict) and isinstance(exclusion.get('reason'), str) and exclusion['reason'].strip()
                     and isinstance(exclusion.get('actual_document'), dict), '排除项须含用户原因和实际N单据证据')
                same_document(exclusion['actual_document'], saved, 'N')
                excluded_bindings.append({**binding,'excluded':True,'exclusion_reason':exclusion['reason'],
                    'actual_status':'N','actual_document':exclusion['actual_document']})
            else:
                need(no not in approvals and no in approved_by_no, '缺少完整已审回执或正式单号重复：' + no)
                approved = approved_by_no[no]
                same_document(approved, saved, 'Y')
                need(approved['DOC_NO'] == no and approved['Version'] >= saved['Version'], '审核回执身份或版本不符')
                bindings.append(binding); approvals[no] = approved
            documents.append({'ID':saved['MO_ID'],'DOC_NO':no})
            created.append({'index':i,'document_no':no,'mo_id':saved['MO_ID']})
        need(set(excluded_by_no) == {b['workorder_no'] for b in excluded_bindings}, '排除清单含非本19来源工单')
        need(creation['documents'] == documents and creation['bindings'] == source_bindings and
             checkpoint['created'] == created and checkpoint['bindings'] == source_bindings, '开单清单或绑定已变化')
        final_bindings = [{**b,'approval_status':'Y'} for b in bindings]
        # Save all full approval documents only after validating the whole scope.
        approval_path = self.directory/('approval-evidence-'+continuation_id+'.json' if derived else 'approval-evidence.json')
        need(not approval_path.exists(), '已存在审核证据，先核实原记录，禁止覆盖')
        save(approval_path, approvals)
        completion = {**creation,'complete':True,'approved_count':len(bindings),'bindings':final_bindings,
                      'excluded_count':len(excluded_bindings),'excluded_bindings':excluded_bindings,
                      'continuation_id':continuation_id,
                      'approval_evidence_sha256':digest(approval_path),
                      'creation_completion_sha256':digest(self.directory/'creation-completion.json')}
        if derived:
            completion['derived_from_checkpoint_sha256'] = digest(self.directory/'checkpoint.json')
            completion['derived_from_creation_completion_sha256'] = digest(self.directory/'creation-completion.json')
        else:
            checkpoint.update({'stage':'approved','confirmed':[v['DOC_NO'] for v in documents],'bindings':final_bindings})
            save(self.directory/'checkpoint.json', checkpoint)
        completion_path = self.directory/('completion-'+continuation_id+'.json' if derived else 'completion.json')
        save(completion_path, completion)
        return {'ok':True,'phase':'manual_complete_after_approval','created':len(packets),'approved':len(bindings),
                'excluded':len(excluded_bindings),'completion':str(completion_path)}


async def run_manual(call, command, base, session_id, emit=print):
    if command.get('job') not in ('manual_prepare', 'manual_create'):
        return {'ok':False,'phase':command.get('job'),'stopped':True,
                'error':'manual_execute/manual_confirm已禁用；仅支持manual_prepare和manual_create，统一审核须等待订单与19来源全部开单完成。',
                'note':'未调用任何MCP，未执行审核。'}
    directory = local(base, command['directory'])
    work = ManualRun(call, base, directory, session_id, emit)
    try:
        if command['job'] == 'manual_prepare':
            return await work.prepare(local(base, command['scope']))
        return await work.create(command['approved_sha256'], local(base, command['source_check']))
    except Exception as exc:
        failure = {'ok':False,'phase':command['job'],'stopped':True,'error':str(exc),
                   'note':'未自动重试；保留准备、started和in_flight，先核对实际结果。'}
        # Never overwrite the evidence of a prior failed/unknown attempt.
        if directory.exists():
            path = directory/('failure-'+uuid.uuid4().hex+'.json'); save(path, failure)
            failure['failure_file'] = str(path)
        return failure
