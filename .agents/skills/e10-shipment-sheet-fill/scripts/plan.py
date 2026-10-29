"""Build a local E10 three-source shipment plan; never performs remote writes."""
import argparse
import json
import math
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

SALES_PREFIXES = ('2409', '2410', '2411', '2412', '2416', '2417', '2502', '2503', '2505')
BORROW_EXCLUDED = {'内部借出单', '借料单(工厂用)'}
BORROW_NAMES = {'1902': '送样借出单', '1904': '备机借出单', '1908': '客户借货单'}
STATUSES = {'不需出库', '未出库', '部分出库', '出库结束'}
MODULES = {'sales', 'borrow', 'outbound_application'}
APPLICATION_PREFIXES = ('1018', '1008', '1012')


def iso_day(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('日期必须为YYYY-MM-DD: ' + str(value))
    return parsed


def text(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (str, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('单元格不能为NaN或Infinity')
        return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
    raise ValueError('单元格必须为标量')


def canonical_key(key):
    if not isinstance(key, str):
        raise ValueError('列名必须为字符串')
    for prefix in ('借出明细_', '出库申请明细_'):
        if key.startswith(prefix):
            return '销货明细_' + key[len(prefix):]
    return key


def source_columns(row):
    columns = {}
    for key, value in row['columns'].items():
        key = canonical_key(key)
        if row['module'] == 'outbound_application':
            key = {'可发货日期': '预计发货日期', '申请人员': '业务员'}.get(key, key)
        if key in columns and text(columns[key]) != text(value):
            raise ValueError('来源字段映射冲突: ' + key)
        text(value)
        columns[key] = value
    for key, value in {'单号': row['doc_no'], '销货明细_品号': row['item_code'],
                       '销货明细_品名': row['item_name'], '交付标签': row.get('delivery_label') or '',
                       '单据类型': row['doc_type_name'], '预计发货日期': row['expected_date']}.items():
        if key in columns and text(columns[key]) != text(value):
            raise ValueError('归一化字段与源字段不一致: ' + row['doc_no'] + '/' + key)
        columns[key] = value
    columns['备注'] = '/'.join(text(value) for value in
                             (columns.get('销货明细_备注'), columns.get('备注'))
                             if value not in (None, ''))
    if row['module'] == 'borrow':
        columns['单据类型'] = BORROW_NAMES.get(row['doc_type_code'], row['doc_type_name'])
    return {key: text(value) for key, value in columns.items()}


def build_plan(data, today):
    today = iso_day(today)
    if data.get('records_complete') is not True or data.get('sheets_complete') is not True:
        raise ValueError('必须完整收集原始明细和全部sheet后生成计划')
    modules = data.get('modules_complete')
    if not isinstance(modules, list) or not modules or len(set(modules)) != len(modules) or not set(modules) <= MODULES:
        raise ValueError('modules_complete须明确列出已完整收集的模块，零记录模块也须声明')
    rows, sheets = data['records'], data['sheets']
    existing, dated, sheet_ids = set(), {}, set()
    for sheet in sheets:
        if sheet['sheet_id'] in sheet_ids:
            raise ValueError('sheetId重复')
        sheet_ids.add(sheet['sheet_id'])
        if not isinstance(sheet['headers'], list) or not all(isinstance(h, str) for h in sheet['headers']):
            raise ValueError('headers必须保留真实列顺序')
        existing.update(text(no).strip() for no in sheet['doc_nos'] if text(no).strip())
        if sheet.get('date') is not None:
            day = iso_day(sheet['date'])
            if day in dated:
                raise ValueError('工作表日期重复，需确认目标表')
            dated[day] = sheet
    groups, ids = defaultdict(list), set()
    for row in rows:
        if row['module'] not in modules or not isinstance(row['doc_no'], str) or not row['doc_no'] or row['doc_no'] != row['doc_no'].strip():
            raise ValueError('模块或完整单号无效')
        rid = (row['module'], row['detail_id'])
        if not row['detail_id'] or rid in ids:
            raise ValueError('明细ID缺失或重复')
        ids.add(rid)
        iso_day(row['doc_date']); iso_day(row['expected_date'])
        if row['module'] == 'sales' and row.get('outbound_status') not in STATUSES:
            raise ValueError('出库状态未按真实元数据归一化')
        if row['module'] == 'borrow' and type(row.get('borrow_detail_ended')) is not bool:
            raise ValueError('借出明细结束须按真实元数据归一为布尔值')
        if row['module'] != 'sales' and row.get('delivery_label') not in (None, ''):
            raise ValueError('借出及出库申请不填交付标签')
        if not row['doc_type_name'] or (row['module'] == 'borrow' and not row['doc_type_code']):
            raise ValueError('缺少真实单据名称或借出单类型代码')
        if not isinstance(row['item_code'], str) or not isinstance(row['item_name'], str):
            raise ValueError('品号、品名必须为字符串')
        source_columns(row)
        groups[row['doc_no']].append(row)
    output, missing, dropped_docs, dropped_details = {}, [], [], []
    counts = {'input_rows': len(rows), 'input_documents': len(groups), 'existing_rows': 0,
              'existing_documents': 0, 'other_filtered_rows': 0, 'detail_filtered_rows': 0,
              'planned_rows': 0, 'planned_documents': 0, 'missing_rows': 0, 'missing_documents': 0}
    for no, detail in groups.items():
        head_keys = ('module', 'doc_date', 'expected_date', 'doc_type_name', 'doc_type_code')
        if any(len({r.get(key) for r in detail}) != 1 for key in head_keys) or (
                detail[0]['module'] == 'sales' and len({r['outbound_status'] for r in detail}) != 1):
            raise ValueError('同单号单头或预计日期不一致: ' + no)
        head = detail[0]
        reason = None
        if head['module'] == 'sales' and no.startswith(SALES_PREFIXES): reason = 'sales_prefix'
        elif head['module'] == 'borrow' and head['doc_type_name'] in BORROW_EXCLUDED: reason = 'borrow_document_name'
        elif head['module'] == 'outbound_application' and not no.startswith(APPLICATION_PREFIXES): reason = 'outbound_application_prefix'
        elif iso_day(head['doc_date']) < date(2026, 1, 1): reason = 'document_date'
        elif head['module'] == 'sales' and head['outbound_status'] == '出库结束': reason = 'outbound_finished'
        elif iso_day(head['expected_date']) < today: reason = 'expected_date_past'
        elif no in existing: reason = 'existing_document'
        if reason:
            dropped_docs.append({'doc_no': no, 'rows': len(detail), 'reason': reason})
            if reason == 'existing_document':
                counts['existing_documents'] += 1; counts['existing_rows'] += len(detail)
            else: counts['other_filtered_rows'] += len(detail)
            continue
        expected = iso_day(head['expected_date'])
        all_s = head['module'] == 'sales' and all(re.fullmatch(r'[sS][0-9]+', text(r.get('delivery_label'))) is not None for r in detail)
        advance = expected > today + timedelta(days=1) and all_s
        ideal = expected - timedelta(days=1) if advance else expected
        target = ideal if ideal in dated else None
        if target is None and advance:
            candidates = [day for day in dated if day <= ideal]
            target = max(candidates) if candidates else None
        kept = []
        for row in detail:
            sku, name = row['item_code'], row['item_name']
            rule = 'E_prefix' if sku.startswith('E') else None
            if row['module'] == 'borrow' and row['borrow_detail_ended']:
                rule = 'borrow_detail_ended'
            if not rule and sku.startswith('1-V') and ('S263' in name or 'S630' in name): rule = '1-V_S263_S630'
            elif sku.startswith('1-H') and 'DSN' in name: rule = '1-H_DSN'
            if rule:
                dropped_details.append({'module': row['module'], 'doc_no': no, 'detail_id': row['detail_id'], 'reason': rule})
                counts['detail_filtered_rows'] += 1
            else: kept.append(row)
        if not kept: continue
        if target is None:
            missing.append({'doc_no': no, 'expected_date': expected.isoformat(), 'ideal_target_date': ideal.isoformat(),
                            'rows': len(kept), 'detail_ids': [r['detail_id'] for r in kept]})
            counts['missing_documents'] += 1; counts['missing_rows'] += len(kept)
            continue
        sheet = dated[target]
        bucket = output.setdefault(sheet['sheet_id'], {'sheet_id': sheet['sheet_id'], 'name': sheet['name'],
                                   'date': target.isoformat(), 'headers': sheet['headers'], 'records': []})
        for row in kept:
            columns = source_columns(row)
            bucket['records'].append((row, [columns.get(canonical_key(header), '') for header in sheet['headers']]))
        counts['planned_documents'] += 1; counts['planned_rows'] += len(kept)
    result_sheets = []
    for bucket in sorted(output.values(), key=lambda s: s['date']):
        ordered = sorted(bucket.pop('records'), key=lambda pair: (pair[0]['expected_date'], pair[0]['doc_no'], pair[0]['detail_id']))
        bucket['values'] = [values for _, values in ordered]
        bucket['source'] = [{'module': row['module'], 'doc_no': row['doc_no'], 'detail_id': row['detail_id']} for row, _ in ordered]
        result_sheets.append(bucket)
    assert counts['input_rows'] == sum(counts[k] for k in ('existing_rows', 'other_filtered_rows', 'detail_filtered_rows', 'planned_rows', 'missing_rows'))
    return {'status': 'prepared_only', 'query_date': today.isoformat(), 'modules_complete': sorted(modules),
            'all_modules_complete': set(modules) == MODULES, 'counts': counts, 'sheets': result_sheets,
            'missing': missing, 'excluded_documents': dropped_docs, 'excluded_details': dropped_details}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--today', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding='utf-8-sig'))
    plan = build_plan(payload, args.today)
    args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps(plan['counts'], ensure_ascii=False))
