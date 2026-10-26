#!/usr/bin/env python3
"""Read-only workflow adapters; source files and online documents are never edited."""
import argparse
import csv
import hashlib
import io
import json
import re
import os
from operator import itemgetter
from pathlib import Path

ORDER_FIELDS = ('包装工单', '物料领料单号', '机器领料单号')
SOURCE_FIELDS = ('领料申请单明细_工单单号', '申请单号', '领料申请单明细_仓库名称', '领料申请单明细_材料品号')
SOURCE_KEYS = ('WORKORDER_NO', 'DOC_NO', 'WAREHOUSE_NAME', 'ITEM_CODE')
SOURCE_STATUS_KEY = 'APPROVAL_STATUS'
SOURCE_VALUES = itemgetter(*SOURCE_KEYS)
RANGE_PATTERN = re.compile(r'A1:[A-Z]+[1-9]\d*')
ORDER_PATTERN = re.compile(r'[A-Za-z0-9]+-[A-Za-z0-9]+')
ORDER_SEPARATOR = re.compile(r'[\r\n/]+')
MATRIX_RANGE = re.compile(r'^A1:([A-Z]+)([1-9]\d*)$')


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def save_json(path, value):
    with Path(path).open('x', encoding='utf-8', newline='') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def outputs(directory, source, names):
    root = Path(directory).resolve()
    skill = Path(__file__).resolve().parents[1]
    if root.is_relative_to(skill):
        raise ValueError('业务结果须写入本轮工作目录，不能放在技能目录')
    result = [root / name for name in names]
    if Path(source).resolve() in result:
        raise ValueError('输出不能覆盖输入')
    if any(path.exists() for path in result):
        raise ValueError('输出文件已存在；拒绝覆盖既有产物')
    root.mkdir(parents=True, exist_ok=True)
    return result


def _column_number(letters):
    number = 0
    for char in letters:
        number = number * 26 + ord(char) - 64
    return number


def _validate_snapshot_matrix(snapshot, expected_sheets=None):
    if (snapshot.get('schema_version') != 1 or snapshot.get('phase') != 'collect'
            or snapshot.get('ok') is not True or snapshot.get('complete') is not True):
        raise ValueError('需要本轮成功且完整的采集快照')
    sheets = snapshot.get('sheets')
    if not isinstance(sheets, list) or not sheets:
        raise ValueError('snapshot工作表不能为空')
    names = [s.get('name') for s in sheets]
    ids = [s.get('sheet_id') for s in sheets]
    if any(not isinstance(name, str) or not name.strip() for name in names) or len(set(names)) != len(names):
        raise ValueError('snapshot工作表名缺失或重复')
    if expected_sheets is not None:
        if (not expected_sheets or len(set(expected_sheets)) != len(expected_sheets)
                or set(names) != set(expected_sheets)):
            raise ValueError('snapshot工作表集合与授权集合不完全一致')
    if any(not isinstance(sid, str) or not sid for sid in ids) or len(set(ids)) != len(ids):
        raise ValueError('snapshot sheet_id缺失或重复')
    for sheet in sheets:
        if sheet.get('scope_complete') is not True:
            raise ValueError('snapshot工作表范围未标记完整：' + str(sheet.get('name')))
        range_text = sheet.get('range', '')
        if not isinstance(range_text, str):
            raise ValueError('snapshot范围格式无效：' + str(sheet.get('name')))
        match = MATRIX_RANGE.fullmatch(range_text)
        if not match:
            raise ValueError('snapshot范围须为完整A1矩阵：' + str(sheet.get('name')))
        tsv = sheet.get('tsv')
        if not isinstance(tsv, str):
            raise ValueError('snapshot TSV缺失或格式无效：' + sheet['name'])
        rows = list(csv.reader(io.StringIO(tsv), delimiter='\t'))
        expected_width, expected_height = _column_number(match.group(1)), int(match.group(2))
        if len(rows) != expected_height or any(len(row) != expected_width for row in rows):
            raise ValueError('snapshot矩阵尺寸与声明范围不一致：' + sheet['name'])
    return sheets


def extract_snapshot_workorders(snapshot, order_column='包装工单', exclusions=None,
                                expected_sheets=None):
    """Collect every non-excluded order value, including completed historical rows."""
    sheets = _validate_snapshot_matrix(snapshot, expected_sheets)
    indexed = validate_exclusions(snapshot, exclusions)
    orders, seen, row_sources, missing_rows = [], set(), [], []
    excluded_rows = []
    for sheet in sheets:
        rows = list(csv.reader(io.StringIO(sheet['tsv']), delimiter='\t'))
        header = None
        for idx, row in enumerate(rows[:20]):
            names = [value.strip() for value in row]
            if order_column in names:
                if names.count(order_column) != 1:
                    raise ValueError('包装工单列重复：' + sheet['name'])
                header = idx
                col = names.index(order_column)
                break
        if header is None:
            raise ValueError('缺少包装工单列：' + sheet['name'])
        for row_no, row in enumerate(rows[header + 1:], header + 2):
            if not any(value.strip() for value in row):
                continue
            exclusion = indexed.get((sheet['name'], row_no))
            if exclusion:
                excluded_rows.append({'sheet': sheet['name'], 'row': row_no,
                                      'identity': exclusion['identity'],
                                      'reason': exclusions['reason']})
                continue
            value = row[col].strip() if col < len(row) else ''
            if not value:
                missing_rows.append({'sheet': sheet['name'], 'row': row_no})
                continue
            parts = [piece.strip() for piece in ORDER_SEPARATOR.split(value) if piece.strip()]
            # Reject the entire cell if any component is malformed; never partially project it.
            if not parts or any(not ORDER_PATTERN.fullmatch(piece) for piece in parts):
                raise ValueError('包装工单单元格含无效工单号：%s!%s' % (sheet['name'], row_no))
            unique_parts = list(dict.fromkeys(parts))
            row_sources.append({'sheet': sheet['name'], 'row': row_no,
                                'workorder_cell': value, 'workorders': unique_parts})
            for part in unique_parts:
                if part not in seen:
                    seen.add(part)
                    orders.append(part)
    return {'workorders': orders, 'row_sources': row_sources,
            'missing_workorder_rows': missing_rows, 'excluded_rows': excluded_rows}


EXCLUSION_IDENTITY = (('单号',),('单身来源','单身来源单号'),('品号','销货明细_品号'),
                      ('特征码','销货明细_特征码'),('数量','销货明细_业务数量'),('包装工单',))


def exclusion_fields(identity):
    if not isinstance(identity,dict): raise ValueError('排除行身份字段不完整')
    chosen=[]
    for aliases in EXCLUSION_IDENTITY:
        found=[name for name in aliases if name in identity]
        if len(found)!=1: raise ValueError('排除行身份字段不完整')
        chosen.append(found[0])
    if set(identity)!=set(chosen): raise ValueError('排除行身份字段含未知字段')
    return tuple(chosen)


def validate_exclusions(snapshot, exclusions):
    if exclusions is None:
        return {}
    if (exclusions.get('schema_version') != 1 or exclusions.get('target_url') != snapshot.get('target_url')
            or not exclusions.get('reason') or not isinstance(exclusions.get('rows'), list)):
        raise ValueError('排除文件版本/目标文档/原因不符')
    indexed = {}
    for item in exclusions['rows']:
        if set(item) != {'sheet','row','identity'} or type(item['row']) is not int or item['row'] < 1:
            raise ValueError('排除行格式不完整')
        fields=exclusion_fields(item['identity'])
        key=(item['sheet'],item['row'])
        if key in indexed: raise ValueError('排除行重复')
        indexed[key]=item
    for sheet in snapshot.get('sheets',[]):
        for number, item in ((n,x) for (s,n),x in indexed.items() if s==sheet['name']):
            rows=list(csv.reader(io.StringIO(sheet['tsv']),delimiter='\t'))
            found=None
            fields=exclusion_fields(item['identity'])
            for h,row in enumerate(rows[:20]):
                names=[v.strip() for v in row]
                if all(k in names for k in fields):
                    if any(names.count(k)!=1 for k in fields): raise ValueError('排除身份表头重复')
                    found=(h,{k:names.index(k) for k in fields});break
            if found is None: raise ValueError('排除行缺少身份表头')
            h,cols=found; offset=number-1
            if offset<=h or offset>=len(rows): raise ValueError('排除行超出快照')
            actual={k:(rows[offset][c] if c<len(rows[offset]) else '') for k,c in cols.items()}
            if actual != item['identity']: raise ValueError('排除行身份变化：%s!%s'%(sheet['name'],number))
    if any(s not in {x['name'] for x in snapshot.get('sheets',[])} for s,n in indexed):
        raise ValueError('排除文件包含未知工作表')
    return indexed


def extract_orders(snapshot, order_column='包装工单', exclusions=None):
    if snapshot.get('schema_version') != 1 or snapshot.get('phase') != 'collect' or snapshot.get('ok') is not True:
        raise ValueError('需要本轮成功的完整采集结果')
    sheets = snapshot.get('sheets', [])
    if not sheets or len({s['name'] for s in sheets}) != len(sheets):
        raise ValueError('工作表为空或重复')
    wanted_fields = (order_column, ORDER_FIELDS[1], ORDER_FIELDS[2])
    exclusion_index=validate_exclusions(snapshot, exclusions)
    orders, selected, missing, invalid, summary = [], [], [], [], {}
    excluded=[]
    seen = set()
    for sheet in sheets:
        if not RANGE_PATTERN.fullmatch(sheet.get('range', '')):
            raise ValueError('采集范围须从A1开始；不能猜测实际行号')
        rows = list(csv.reader(io.StringIO(sheet['tsv']), delimiter='\t'))
        header, columns = None, None
        for r, row in enumerate(rows[:20]):
            names = [v.strip() for v in row]
            if all(field in names for field in wanted_fields):
                if any(names.count(field) != 1 for field in wanted_fields):
                    raise ValueError('匹配表头不唯一：' + sheet['name'])
                header, columns = r, {field:names.index(field) for field in wanted_fields}
                break
        if header is None:
            raise ValueError('缺少领料检查表头：' + sheet['name'])
        order_index, material_index, machine_index = (columns[field] for field in wanted_fields)
        counts = {'business_rows':0, 'selected_rows':0, 'missing_workorder_rows':0, 'invalid_workorder_rows':0}
        for r, raw in enumerate(rows[header + 1:], header + 2):
            excluded_item=exclusion_index.get((sheet['name'],r))
            if excluded_item:
                excluded.append({'sheet':sheet['name'],'row':r,'reason':exclusions['reason'],'identity':excluded_item['identity']})
                continue
            if not any(v.strip() for v in raw):
                continue
            counts['business_rows'] += 1
            width = len(raw)
            order_value = raw[order_index] if order_index < width else ''
            material_value = raw[material_index] if material_index < width else ''
            machine_value = raw[machine_index] if machine_index < width else ''
            empty_fields = []
            if not material_value.strip():
                empty_fields.append(ORDER_FIELDS[1])
            if not machine_value.strip():
                empty_fields.append(ORDER_FIELDS[2])
            if not empty_fields:
                continue
            item = {'sheet':sheet['name'], 'row':r, 'workorder_cell':order_value,
                    'material_before':material_value, 'machine_before':machine_value,
                    'empty_fields':empty_fields}
            parts = [v.strip() for v in ORDER_SEPARATOR.split(item['workorder_cell']) if v.strip()]
            if not parts:
                missing.append(item)
                counts['missing_workorder_rows'] += 1
                continue
            if any(not ORDER_PATTERN.fullmatch(part) for part in parts):
                invalid.append(item)
                counts['invalid_workorder_rows'] += 1
                continue
            item['orders'] = list(dict.fromkeys(parts))
            selected.append(item)
            counts['selected_rows'] += 1
            for order in item['orders']:
                if order not in seen:
                    seen.add(order)
                    orders.append(order)
        summary[sheet['name']] = counts
    return {'schema_version':1, 'target_url':snapshot.get('target_url'), 'batch_id':snapshot.get('batch_id'),
            'orders':orders, 'order_count':len(orders), 'order_text':';'.join(orders), 'rows':selected,
            'missing_workorder_rows':missing, 'invalid_workorder_rows':invalid, 'sheets':summary,
            'excluded_rows':excluded}


SUPPORTED_SOURCE_FILTERS = {
    'date_from': 'DOC_DATE',
    'detail_close': 'DETAIL_CLOSE',
    'approval_status': SOURCE_STATUS_KEY,
    'applicant_name': 'APPLICANT_NAME',
    'workorder_no': 'WORKORDER_NO',
    'workorder_nos': 'WORKORDER_NO',
}


def _validate_source_filters(filters, allow_workorder_scope=True):
    if not isinstance(filters, dict) or set(filters) - set(SUPPORTED_SOURCE_FILTERS):
        raise ValueError('查询filters格式无效或含不支持字段')
    nos = filters.get('workorder_nos')
    single = filters.get('workorder_no')
    if nos is not None:
        if not allow_workorder_scope:
            raise ValueError('本处不接受工单列表筛选')
        if single:
            raise ValueError('workorder_no与workorder_nos不能同时指定')
        if not isinstance(nos, list) or not 1 <= len(nos) <= 500:
            raise ValueError('workorder_nos必须为1至500个完整工单号')
        if any(not isinstance(no, str) or not ORDER_PATTERN.fullmatch(no) for no in nos):
            raise ValueError('workorder_nos含格式无效的完整工单号')
        if len(set(nos)) != len(nos):
            raise ValueError('workorder_nos不能重复')
    if single and (not isinstance(single, str) or not ORDER_PATTERN.fullmatch(single)):
        raise ValueError('workorder_no仅接受单个完整工单号')
    return filters


def _validate_query_filter_row(row, filters):
    _validate_source_filters(filters)
    for filter_name, field in SUPPORTED_SOURCE_FILTERS.items():
        expected = filters.get(filter_name)
        if expected is None or expected == '':
            continue
        actual = row.get(field)
        if filter_name == 'date_from':
            if str(actual or '')[:10] < str(expected):
                raise ValueError('查询日期条件与结果不符')
        elif filter_name == 'workorder_nos':
            if str(actual or '').strip() not in expected:
                raise ValueError('列表查询返回列表外工单：' + str(actual))
        elif str(actual or '').strip() != str(expected).strip():
            raise ValueError('原始查询筛选条件与结果不符：' + filter_name)


def _normalize_source(query, scoped_workorders=None):
    if query.get('ok') is not True or query.get('complete') is not True or query.get('has_next_page') is not False:
        raise ValueError('查询尚未确认分页完整，不能生成匹配源')
    if query.get('include_detail') is not True:
        raise ValueError('需要领料申请单明细，不能用单头替代')
    filters = query.get('filters', {})
    if not isinstance(filters, dict):
        raise ValueError('源查询filters格式无效')
    _validate_source_filters(filters)
    source_rows = query.get('rows')
    if not isinstance(source_rows, list):
        raise ValueError('源查询rows格式无效')
    seen, documents, result, missing = set(), set(), [], 0
    includes_status = any(SOURCE_STATUS_KEY in row for row in source_rows)
    status_counts, excluded_void, unapproved = {}, [], []
    scoped_rows, scoped_query_rows, scoped_documents = [], [], set()
    scoped_status_counts, scoped_void, scoped_unapproved, out_of_scope_status = {}, [], [], []
    missing_workorder_count = 0
    for row in source_rows:
        key = (row.get('ID'), row.get('DETAIL_ID'))
        if not key[0] or not key[1] or key in seen:
            raise ValueError('明细ID缺失或重复，须核实分页')
        seen.add(key)
        _validate_query_filter_row(row, filters)
        try:
            raw_values = SOURCE_VALUES(row)
        except KeyError:
            raw_values = tuple(row.get(field) for field in SOURCE_KEYS)
        values = ['' if value is None else str(value) for value in raw_values]
        missing += (not values[0].strip() or not values[1].strip()
                    or not values[2].strip() or not values[3].strip())
        if values[1]:
            documents.add(values[1])
        workorder = '' if row.get('WORKORDER_NO') is None else str(row.get('WORKORDER_NO')).strip()
        status = '' if row.get(SOURCE_STATUS_KEY) is None else str(row.get(SOURCE_STATUS_KEY)).strip()
        is_scoped = scoped_workorders is not None and workorder in scoped_workorders
        if not workorder:
            missing_workorder_count += 1
        if is_scoped:
            scoped_values = list(values)
            scoped_status_counts[status] = scoped_status_counts.get(status, 0) + 1
            if status == 'V':
                scoped_void.append({'ID':row.get('ID'),'DETAIL_ID':row.get('DETAIL_ID'),
                    'DOC_NO':values[1],'WORKORDER_NO':values[0],'APPROVAL_STATUS':status})
            elif status != 'Y':
                item={'ID':row.get('ID'),'DETAIL_ID':row.get('DETAIL_ID'),
                      'DOC_NO':values[1],'WORKORDER_NO':values[0],'APPROVAL_STATUS':status}
                if not includes_status:
                    item['reason'] = 'approval_status_field_missing'
                scoped_unapproved.append(item)
            if includes_status:
                scoped_values.append(status)
            scoped_rows.append(scoped_values)
            scoped_query_rows.append(row)
            if values[1]:
                scoped_documents.add(values[1])
        elif status == 'N' or status == '' or status not in ('Y', 'V', 'N'):
            out_of_scope_status.append({'ID':row.get('ID'),'DETAIL_ID':row.get('DETAIL_ID'),
                'WORKORDER_NO':workorder,'APPROVAL_STATUS':status,
                'classification':'N' if status == 'N' else ('empty' if not status else 'unknown')})
        if includes_status:
            status_counts[status] = status_counts.get(status, 0) + 1
            if status == 'V':
                excluded_void.append({'ID':row.get('ID'),'DETAIL_ID':row.get('DETAIL_ID'),
                    'DOC_NO':values[1],'WORKORDER_NO':values[0],'APPROVAL_STATUS':status})
            elif status != 'Y':
                unapproved.append({'ID':row.get('ID'),'DETAIL_ID':row.get('DETAIL_ID'),
                    'DOC_NO':values[1],'WORKORDER_NO':values[0],'APPROVAL_STATUS':status})
            values.append(status)
        result.append(values)
    return result, {'records':len(result), 'documents':len(documents),
                    'missing_field_records':missing, 'filters':filters,
                    'includes_approval_status':includes_status,'approval_status_counts':status_counts,
                    'void_excluded_records':excluded_void,'unapproved_or_unknown_records':unapproved,
                    'scoped_rows':scoped_rows,'scoped_query_rows':scoped_query_rows,
                    'scoped_documents':len(scoped_documents),
                    'scoped_approval_status_counts':scoped_status_counts,
                    'scoped_void_excluded_records':scoped_void,
                    'scoped_unapproved_or_unknown_records':scoped_unapproved,
                    'range_outside_missing_workorder_row_count':missing_workorder_count,
                    'range_outside_n_or_unknown_evidence':out_of_scope_status}


def normalize_source(query):
    rows, summary = _normalize_source(query)
    # Keep the public four-field/legacy source API unchanged.
    summary = {key: value for key, value in summary.items()
               if not key.startswith('scoped_') and key not in ('range_outside_missing_workorder_row_count',
                                                                 'range_outside_n_or_unknown_evidence')}
    return rows, summary


def _project_source_query(query, snapshot, exclusions=None, query_path=None,
                          snapshot_path=None, exclusions_path=None,
                          order_column='包装工单', expected_sheets=None):
    """Validate the complete remote query, then make an exact local workorder projection."""
    # Validate the snapshot authorization and extract every in-scope workorder first.
    extracted = extract_snapshot_workorders(snapshot, order_column, exclusions, expected_sheets)
    target_set = set(extracted['workorders'])
    query_filters = query.get('filters', {})
    _validate_source_filters(query_filters)
    declared_workorders = query_filters.get('workorder_nos')
    if declared_workorders is not None and set(declared_workorders) != target_set:
        raise ValueError('源查询workorder_nos与快照完整目标工单集合不一致')
    # One complete pass validates IDs and every actual remote filter while classifying target rows.
    _, normalized = _normalize_source(query, target_set)
    selected = normalized['scoped_query_rows']
    projection = {key: value for key, value in query.items() if key != 'rows'}
    projection['rows'] = selected
    projection['snapshot_basis'] = 'local_target_workorder_projection'
    query_path_value = str(Path(query_path).resolve()) if query_path else None
    query_sha = file_sha256(query_path) if query_path else None
    projection['source_query_file'] = query_path_value
    projection['source_query_sha256'] = query_sha
    projection['local_scope'] = {
        'snapshot_file': str(Path(snapshot_path).resolve()) if snapshot_path else None,
        'snapshot_sha256': file_sha256(snapshot_path) if snapshot_path else None,
        'exclusions_file': str(Path(exclusions_path).resolve()) if exclusions_path else None,
        'exclusions_sha256': file_sha256(exclusions_path) if exclusions_path else None,
        'authorized_sheets': list(expected_sheets) if expected_sheets is not None else [s['name'] for s in snapshot['sheets']],
        'workorders': extracted['workorders'],
        'workorder_sources': extracted['row_sources'],
        'excluded_snapshot_rows': extracted['excluded_rows'],
        'missing_workorder_snapshot_rows': extracted['missing_workorder_rows'],
        'input_row_count': len(query['rows']),
        'target_row_count': len(selected),
        'range_outside_workorder_row_count': len(query['rows']) - len(selected),
        'range_outside_missing_workorder_row_count': normalized['range_outside_missing_workorder_row_count'],
        'range_outside_n_or_unknown_evidence': normalized['range_outside_n_or_unknown_evidence'],
        'remote_query_filters_unchanged': True,
        'local_projection_only': True,
    }
    blockers = normalized['scoped_unapproved_or_unknown_records']
    summary = {
        'records': len(normalized['scoped_rows']),
        'documents': normalized['scoped_documents'],
        'missing_field_records': sum(not all(str(row.get(field) or '').strip() for field in SOURCE_KEYS)
                                     for row in selected),
        'filters': query.get('filters', {}),
        'includes_approval_status': normalized['includes_approval_status'],
        'approval_status_counts': normalized['scoped_approval_status_counts'],
        'void_excluded_records': normalized['scoped_void_excluded_records'],
        'unapproved_or_unknown_records': blockers,
        'source_snapshot_basis': projection['snapshot_basis'],
        'input_row_count': len(query['rows']),
        'target_row_count': len(selected),
        'workorder_count': len(extracted['workorders']),
        'workorders': extracted['workorders'],
        'missing_workorder_snapshot_rows': extracted['missing_workorder_rows'],
        'excluded_snapshot_rows': extracted['excluded_rows'],
        'range_outside_workorder_row_count': len(query['rows']) - len(selected),
        'range_outside_missing_workorder_row_count': normalized['range_outside_missing_workorder_row_count'],
        'range_outside_n_or_unknown_count': len(normalized['range_outside_n_or_unknown_evidence']),
        'range_outside_n_or_unknown_evidence': normalized['range_outside_n_or_unknown_evidence'],
        'source_query_file': query_path_value,
        'source_query_sha256': query_sha,
        'source_path_sha256': query_sha,
        'authorized_sheets': projection['local_scope']['authorized_sheets'],
        'source_output_blocked': bool(blockers),
    }
    if blockers:
        summary['blocking_reason'] = '目标工单范围内含N/空/未知审核状态；完整投影已保留，但禁止生成匹配TSV'
    elif not selected:
        summary['empty_target_scope'] = True
        summary['blocking_reason'] = None
    return projection, summary, normalized['scoped_rows']


def project_source_query(query, snapshot, exclusions=None, query_path=None,
                         snapshot_path=None, exclusions_path=None,
                         order_column='包装工单', expected_sheets=None):
    """Public projection API; retains its two-value return contract."""
    projection, summary, _ = _project_source_query(
        query, snapshot, exclusions, query_path, snapshot_path, exclusions_path,
        order_column, expected_sheets)
    return projection, summary


def _write_source_outputs(query_path, out_dir, snapshot_path=None, exclusions_path=None,
                          snapshot=None, exclusions=None, order_column='包装工单',
                          expected_sheets=None):
    names = ('picking-target-query.json', 'picking-source.tsv', 'source-summary.json') if snapshot_path else ('picking-source.tsv', 'source-summary.json')
    root = Path(out_dir).resolve()
    skill = Path(__file__).resolve().parents[1]
    if root.is_relative_to(skill):
        raise ValueError('业务结果须写入本轮工作目录，不能放在技能目录')
    paths = [root / name for name in names]
    inputs = [Path(query_path).resolve()]
    if snapshot_path: inputs.append(Path(snapshot_path).resolve())
    if exclusions_path: inputs.append(Path(exclusions_path).resolve())
    if any(path.resolve() in inputs for path in paths):
        raise ValueError('输出不能覆盖输入')
    if any(path.exists() for path in paths):
        raise ValueError('输出文件已存在；拒绝覆盖既有产物')
    root.mkdir(parents=True, exist_ok=True)
    query = read_json(query_path)
    if snapshot_path:
        projection, summary, rows_for_tsv = _project_source_query(
            query, snapshot, exclusions, query_path, snapshot_path,
            exclusions_path, order_column, expected_sheets)
        save_json(paths[0], projection)
        tsv_path, summary_path = paths[1], paths[2]
    else:
        rows, summary = normalize_source(query)
        tsv_path, summary_path = paths[0], paths[1]
        summary['source_output_blocked'] = bool(summary['unapproved_or_unknown_records'])
        if summary['source_output_blocked']:
            summary['blocking_reason'] = '源记录含N/空/未知审核状态；禁止生成匹配TSV'
        projection, rows_for_tsv = query, (None if summary['source_output_blocked'] else rows)
    if snapshot_path and summary.get('source_output_blocked'):
        rows_for_tsv = None
    if rows_for_tsv is not None:
        with tsv_path.open('x', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream, delimiter='\t')
            writer.writerow((*SOURCE_FIELDS, SOURCE_STATUS_KEY) if summary['includes_approval_status'] else SOURCE_FIELDS)
            writer.writerows(rows_for_tsv)
        summary['source_file'] = str(tsv_path)
        summary['source_file_sha256'] = file_sha256(tsv_path)
    else:
        summary['source_file'] = None
    save_json(summary_path, summary)
    return summary, {'projection': paths[0] if snapshot_path else None,
                     'source': tsv_path if rows_for_tsv is not None else None,
                     'summary': summary_path, 'blocked': summary['source_output_blocked']}


def _source_request(snapshot_path, sheets, exclusions_path, filters_path, out_dir,
                    workorder_column='包装工单'):
    snapshot_path = Path(snapshot_path).resolve()
    run_root = snapshot_path.parent
    output_dir = Path(out_dir).resolve()
    if not output_dir.is_relative_to(run_root) or output_dir == run_root:
        raise ValueError('source-request产物须位于snapshot所在本轮目录内')
    expected = list(sheets)
    if (not expected or any(not isinstance(x, str) or not x.strip() for x in expected)
            or len(set(expected)) != len(expected)):
        raise ValueError('授权工作表须非空且无重复')
    snapshot = read_json(snapshot_path)
    _validate_snapshot_matrix(snapshot, expected)
    exclusions = read_json(exclusions_path) if exclusions_path else None
    extracted = extract_snapshot_workorders(snapshot, workorder_column, exclusions, expected)
    if extracted['missing_workorder_rows']:
        raise ValueError('非空业务行缺少包装工单；拒绝生成不完整查询请求')
    if len(extracted['workorders']) > 500:
        raise ValueError('目标工单数超过单次工具上限500；未静默截断或拆批')
    supplied_filters = read_json(filters_path) if filters_path else {}
    if supplied_filters:
        _validate_source_filters(supplied_filters)
    if 'workorder_nos' in supplied_filters:
        raise ValueError('filters文件不得预设workorder_nos；列表由当前快照范围生成')
    if supplied_filters.get('workorder_no'):
        raise ValueError('filters中的单工单条件与本次精确工单列表冲突')
    filters = dict(supplied_filters)
    if extracted['workorders']:
        filters['workorder_nos'] = extracted['workorders']
        _validate_source_filters(filters)
    target_scope = {
        'schema_version': 1, 'phase': 'source_request', 'target_url': snapshot['target_url'],
        'snapshot_path': str(snapshot_path), 'snapshot_sha256': file_sha256(snapshot_path),
        'exclusions_path': str(Path(exclusions_path).resolve()) if exclusions_path else None,
        'exclusions_sha256': file_sha256(exclusions_path) if exclusions_path else None,
        'filters_path': str(Path(filters_path).resolve()) if filters_path else None,
        'filters_sha256': file_sha256(filters_path) if filters_path else None,
        'authorized_sheets': expected, 'workorder_column': workorder_column,
        'workorders': extracted['workorders'], 'workorder_sources': extracted['row_sources'],
        'missing_workorder_rows': extracted['missing_workorder_rows'],
        'excluded_rows': extracted['excluded_rows'], 'query_filters': filters,
        'no_requests': not bool(extracted['workorders']),
    }
    paths = [output_dir / 'target-scope.json']
    if extracted['workorders']:
        paths.append(output_dir / 'command.json')
    if output_dir.exists() or any(path.exists() for path in paths):
        raise ValueError('source-request输出已存在；拒绝覆盖')
    output_dir.mkdir(parents=True, exist_ok=True)
    save_json(paths[0], target_scope)
    command = None
    if extracted['workorders']:
        def rel(path):
            resolved = Path(path).resolve()
            if not resolved.is_relative_to(run_root):
                raise ValueError('查询路径须位于本轮snapshot目录内')
            return resolved.relative_to(run_root).as_posix()
        command = {'job':'collect', 'filters':filters, 'include_detail':True,
                   'pages':rel(output_dir / 'pages'),
                   'output':rel(output_dir / 'complete.json')}
        save_json(paths[1], command)
    return {'ok':True,'no_requests':not bool(extracted['workorders']),
            'workorder_count':len(extracted['workorders']),
            'command_file':str(paths[1]) if command else None,
            'target_scope_file':str(paths[0]),'workorders':extracted['workorders']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    orders = sub.add_parser('orders')
    orders.add_argument('--snapshot', required=True)
    orders.add_argument('--workorder-column', default='包装工单')
    orders.add_argument('--exclusions')
    orders.add_argument('--out-dir', required=True)
    source = sub.add_parser('source')
    source.add_argument('--query', required=True)
    source.add_argument('--snapshot')
    source.add_argument('--exclusions')
    source.add_argument('--sheets', help='snapshot授权工作表名，逗号分隔')
    source.add_argument('--workorder-column', default='包装工单')
    source.add_argument('--out-dir', required=True)
    source_request = sub.add_parser('source-request')
    source_request.add_argument('--snapshot', required=True)
    source_request.add_argument('--sheets', required=True, help='授权工作表名，逗号分隔')
    source_request.add_argument('--exclusions')
    source_request.add_argument('--filters', help='包含原始查询条件的JSON对象')
    source_request.add_argument('--workorder-column', default='包装工单')
    source_request.add_argument('--out-dir', required=True)
    args = parser.parse_args()
    if args.command == 'orders':
        paths = outputs(args.out_dir, args.snapshot, ('orders.json','workorders.txt'))
        result = extract_orders(read_json(args.snapshot), args.workorder_column,
                                read_json(args.exclusions) if args.exclusions else None)
        save_json(paths[0], result)
        paths[1].write_text(result['order_text'], encoding='utf-8')
        print(json.dumps({'order_count':result['order_count'], 'selected_rows':len(result['rows']),
                          'missing_workorder_rows':len(result['missing_workorder_rows']),
                          'invalid_workorder_rows':len(result['invalid_workorder_rows']),
                          'orders_file':str(paths[0]), 'text_file':str(paths[1])}, ensure_ascii=False))
    elif args.command == 'source-request':
        result = _source_request(args.snapshot, [x.strip() for x in args.sheets.split(',')],
                                 args.exclusions, args.filters, args.out_dir,
                                 args.workorder_column)
        print(json.dumps(result, ensure_ascii=False))
    else:
        if args.exclusions and not args.snapshot:
            raise ValueError('--exclusions需要与--snapshot同时使用')
        if args.sheets and not args.snapshot:
            raise ValueError('--sheets需要与--snapshot同时使用')
        expected_sheets = None
        if args.snapshot:
            if not args.sheets:
                raise ValueError('带--snapshot时必须提供--sheets授权工作表集合')
            expected_sheets = [name.strip() for name in args.sheets.split(',')]
            if any(not name for name in expected_sheets) or len(set(expected_sheets)) != len(expected_sheets):
                raise ValueError('--sheets必须是非空且无重复的逗号分隔集合')
        snapshot = read_json(args.snapshot) if args.snapshot else None
        exclusions = read_json(args.exclusions) if args.exclusions else None
        summary, paths = _write_source_outputs(args.query, args.out_dir,
                                                args.snapshot, args.exclusions,
                                                snapshot, exclusions,
                                                args.workorder_column, expected_sheets)
        print(json.dumps({'records':summary.get('records'),
                          'input_row_count':summary.get('input_row_count'),
                          'target_row_count':summary.get('target_row_count'),
                          'workorder_count':summary.get('workorder_count'),
                          'approval_status_counts':summary.get('approval_status_counts'),
                          'unapproved_or_unknown_count':len(summary.get('unapproved_or_unknown_records',[])),
                          'range_outside_n_or_unknown_count':summary.get('range_outside_n_or_unknown_count'),
                          'source_output_blocked':paths['blocked'],
                          'projection_file':str(paths['projection']) if paths['projection'] else None,
                          'source_file':str(paths['source']) if paths['source'] else None,
                          'summary_file':str(paths['summary'])}, ensure_ascii=False))
        if paths['blocked']:
            raise SystemExit(2)


if __name__ == '__main__':
    main()
