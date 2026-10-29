"""Validate bound shipment pages and planner input; offline integrity, not origin attestation."""
import hashlib
import importlib.util
import json
from pathlib import Path

MODULES = {'sales': 'SALES_DELIVERY', 'borrow': 'BORROW_DOC', 'outbound_application': 'STOCK_REQUISITION'}

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def need(condition, message):
    if not condition:
        raise ValueError(message)

def page_value(value):
    need(isinstance(value, dict) and not value.get('isError'), 'Failed page response')
    if 'structuredContent' in value:
        return value['structuredContent']
    if 'content' in value:
        return json.loads('\n'.join(x['text'] for x in value['content'] if x.get('type') == 'text'))
    return value

def pointer(value, path):
    need(isinstance(path, str) and (path == '' or path.startswith('/')), 'Invalid metadata pointer')
    for part in path.split('/')[1:]:
        part = part.replace('~1','/').replace('~0','~')
        need(isinstance(value, dict) and part in value, 'Metadata pointer missing')
        value = value[part]
    return value

def metadata_options(data, input_path, field, options_name, ref_name):
    ref = data.get(ref_name)
    need(isinstance(ref, dict) and isinstance(ref.get('path'), str), 'Missing raw option evidence')
    path = Path(ref['path'])
    if not path.is_absolute(): path = input_path.parent / path
    need(path.is_file() and digest(path) == ref.get('sha256'), 'Option evidence hash mismatch')
    actual = page_value(read(path))
    item = pointer(actual, ref.get('pointer'))
    options = data.get(options_name)
    need(isinstance(item, dict) and item.get('module') == ('BORROW_DOC' if field == 'CLOSE' else 'SALES_DELIVERY') and item.get('field') == field and item.get('options') == options and isinstance(options, dict) and options and all(isinstance(k,str) and k and isinstance(v,str) and v for k,v in options.items()), 'Raw option evidence mismatch')
    return options

def validate(source):
    binding = source.get('source_binding') or {}
    input_path = Path(binding.get('input_path', ''))
    need(input_path.is_file() and digest(input_path) == binding.get('input_sha256'), 'Missing or changed planner input')
    data = read(input_path)
    kind, scope = data.get('workflow_kind'), data.get('scope')
    need(kind in ('packing_order', 'shipment_fill'), 'Explicit workflow_kind required')
    need(isinstance(scope, list) and scope and len(set(scope)) == len(scope) and set(scope) <= set(MODULES), 'Explicit valid scope required')
    need(kind != 'packing_order' or set(scope) == set(MODULES), 'Complete packing order requires three sources')
    need(set(data.get('modules_complete', [])) == set(scope), 'Scope differs from planner modules')
    evidence = data.get('source_evidence')
    need(isinstance(evidence, dict) and set(evidence) == set(scope), 'Missing source module evidence')
    if 'sales' in scope:
        options = metadata_options(data,input_path,'APPROVAL_STATUS','sales_approval_options','sales_approval_metadata')
        need('作废' in options.values(), 'Missing void option')
        issued = metadata_options(data,input_path,'ISSUED_STATUS','sales_issued_options','sales_issued_metadata')
    if 'borrow' in scope:
        close = metadata_options(data,input_path,'CLOSE','borrow_close_options','borrow_close_metadata')
        need(set(close.values()) <= {'未结束','指定结束','已结束'} and '未结束' in close.values(), 'Unsupported borrow CLOSE options')
    bindings = data.get('field_bindings')
    need(isinstance(bindings,dict) and set(bindings)==set(scope), 'Missing field bindings')
    raw_ids = set()
    raw_by_id = {}
    for module in scope:
        refs = evidence[module]
        need(isinstance(refs, list) and refs, 'Every module including empty modules needs pages')
        start, filters = 1, None
        mapping = bindings[module]
        required = {'doc_type_name','doc_type_code','item_code','item_name'}
        if module == 'sales': required |= {'delivery_label','outbound_status'}
        if module == 'borrow': required |= {'borrow_detail_ended'}
        need(isinstance(mapping,dict) and set(mapping)=={'fields','columns'} and isinstance(mapping['fields'],dict) and set(mapping['fields'])==required and isinstance(mapping['columns'],dict) and all(isinstance(k,str) and k and isinstance(v,str) and v for k,v in {**mapping['fields'],**mapping['columns']}.items()), 'Incomplete field binding')
        canonical = {'doc_type_name':'DOC_NAME','doc_type_code':'DOC_CODE','item_code':'ITEM_CODE','item_name':'ITEM_DESCRIPTION'}
        if module == 'sales': canonical.update(delivery_label='DELIVERY_LABEL',outbound_status='ISSUED_STATUS')
        if module == 'borrow': canonical.update(borrow_detail_ended='CLOSE')
        need(mapping['fields'] == canonical, 'Unexpected semantic alias binding')
        for i, ref in enumerate(refs):
            need(isinstance(ref, dict) and isinstance(ref.get('path'), str), 'Invalid page reference')
            request_ref = ref.get('request')
            need(isinstance(request_ref,dict) and isinstance(request_ref.get('path'),str), 'Missing original request')
            request_path=Path(request_ref['path'])
            if not request_path.is_absolute(): request_path=input_path.parent/request_path
            need(request_path.is_file() and digest(request_path)==request_ref.get('sha256'), 'Request hash mismatch')
            request=read(request_path)
            need(request.get('name')=='e10_query_shipment_details' and isinstance(request.get('arguments'),dict), 'Invalid shipment request')
            args=request['arguments']; fields=args.get('fields')
            need(args.get('module')==MODULES[module] and args.get('start_row',1)==start and isinstance(fields,list), 'Request module or page mismatch')
            need(all(isinstance(x,dict) and isinstance(x.get('alias'),str) and isinstance(x.get('path'),str) for x in fields), 'Invalid request fields')
            aliases={x['alias']:x['path'] for x in fields}
            need(len(aliases)==len(fields), 'Duplicate request alias')
            need(all(aliases.get(k)==v for k,v in {'HEAD_ID':MODULES[module]+'_ID','DETAIL_ID':MODULES[module]+'_D.'+MODULES[module]+'_D_ID','DOC_NO':'DOC_NO','DOC_DATE':'DOC_DATE'}.items()), 'Unbound identity paths')
            need(aliases.get('EXPECTED_DATE')==args.get('expected_path') and (set(mapping['fields'].values()) | set(mapping['columns'].values())) <= (set(aliases) | {'CREATOR_NAME'}), 'Unbound source field')
            if module=='sales': need(aliases.get('APPROVAL_STATUS')=='APPROVAL_STATUS' and aliases.get(mapping['fields']['outbound_status'])=='ISSUED_STATUS', 'Unbound sales state paths')
            if module=='borrow': need(aliases.get(mapping['fields']['borrow_detail_ended'])=='BORROW_DOC_D.CLOSE', 'Unbound borrow close path')
            path = Path(ref['path'])
            if not path.is_absolute(): path = input_path.parent / path
            need(path.is_file() and digest(path) == ref.get('sha256'), 'Page hash mismatch')
            page = page_value(read(path))
            need(isinstance(page, dict) and page.get('ok') is True and page.get('read_only') is True and page.get('module') == MODULES[module], 'Failed or wrong source page')
            f = page.get('filters')
            need(isinstance(f, dict) and f.get('doc_date_from') == '2026-01-01' and f.get('expected_from') == source.get('query_date') and isinstance(f.get('expected_path'), str) and bool(f['expected_path']), 'Query filter mismatch')
            need(all(args.get(k) == f.get(k) for k in ('expected_from','expected_path')) and (args.get('status_path') or None)==(f.get('status_path') or None) and (args.get('status_end') or None)==(f.get('status_end') or None), 'Request/response query mismatch')
            if module == 'sales': need(f.get('status_path') == 'ISSUED_STATUS' and f.get('status_end') == '3', 'Sales filter mismatch')
            elif module == 'borrow': need(f.get('borrow_category') == '19' and not f.get('status_path') and not f.get('status_end'), 'Borrow filter mismatch')
            else: need(f.get('stock_requisition_prefixes') == ['1018','1008','1012'] and not f.get('status_path') and not f.get('status_end'), 'Application filter mismatch')
            if filters is None: filters = f
            need(f == filters, 'Page filters differ')
            rows = page.get('rows')
            need(isinstance(rows, list) and type(page.get('start_row')) is int and page['start_row'] == start and type(page.get('next_start_row')) is int and page['next_start_row'] == start + len(rows), 'Broken pagination')
            more = page.get('has_next_page')
            need(type(more) is bool and more == (i < len(refs)-1) and (not more or len(rows) > 0), 'Missing terminal page or no progress')
            start = page['next_start_row']
            for row in rows:
                need(isinstance(row, dict) and all(isinstance(row.get(k), str) and row[k] for k in ('HEAD_ID','DETAIL_ID','DOC_NO')), 'Missing raw identity')
                key = (module, row['HEAD_ID'], row['DETAIL_ID'], row['DOC_NO'])
                need(key not in raw_ids, 'Duplicate raw identity')
                raw_ids.add(key)
                raw_by_id[key] = row
    normalized = set()
    for row in data['records']:
        key = (row.get('module'), row.get('head_id'), row.get('detail_id'), row.get('doc_no'))
        need(all(isinstance(v, str) and v for v in key) and key not in normalized, 'Invalid normalized identity')
        normalized.add(key)
        if key in raw_by_id:
            raw = raw_by_id[key]
            if row['module'] == 'sales':
                need(isinstance(raw.get('APPROVAL_STATUS'), str) and raw['APPROVAL_STATUS'] == row.get('approval_status'), 'Raw sales approval status differs from normalized input')
            for raw_name, normalized_name in (('DOC_DATE','doc_date'), ('EXPECTED_DATE','expected_date')):
                need(isinstance(raw.get(raw_name), str) and raw[raw_name][:10] == row.get(normalized_name), 'Raw date differs from normalized input')
            mapping=bindings[row['module']]
            for name,alias in mapping['fields'].items():
                need(alias in raw, 'Missing raw field '+alias)
                value=raw[alias]
                if name=='outbound_status':
                    need(isinstance(value,str) and value in issued, 'Unknown raw issued status'); value=issued[value]
                elif name=='borrow_detail_ended':
                    need(isinstance(value,str) and value in close, 'Unknown raw close status'); value=close[value] in ('指定结束','已结束')
                else:
                    value='' if value is None else str(value)
                need(value==row.get(name), 'Raw field differs from normalized input: '+name)
            columns=row.get('columns')
            need(isinstance(columns,dict) and set(columns)==set(mapping['columns']), 'Unbound normalized columns')
            for name,alias in mapping['columns'].items():
                need(alias in raw and raw[alias] == columns[name] and type(raw[alias]) is type(columns[name]), 'Raw column differs from normalized input: '+name)
    need(raw_ids == normalized and len(raw_ids) == len(data['records']), 'Raw and normalized records differ')
    planner_path = Path(__file__).resolve().parents[2] / 'e10-shipment-sheet-fill' / 'scripts' / 'plan.py'
    spec = importlib.util.spec_from_file_location('bound_shipment_planner', planner_path)
    planner = importlib.util.module_from_spec(spec); spec.loader.exec_module(planner)
    rebuilt = planner.build_plan(data, source['query_date'])
    need(all(source.get(key) == value for key, value in rebuilt.items()), 'Planner output differs from bound input')
    return {'workflow_kind': kind, 'scope': sorted(scope), 'input_sha256': binding['input_sha256']}
