#!/usr/bin/env python3
"""Multi-sheet planning, scoped browser stages and reports; no browser connection here."""
import argparse
import csv
import hashlib
import importlib.util
import io
import json
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
KIND = 'workorder' if (HERE / 'match_workorders.py').exists() else 'picking'


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def engine():
    file = HERE / ('match_workorders.py' if KIND == 'workorder' else 'match_picking.py')
    spec = importlib.util.spec_from_file_location('packaging_matching_engine', file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical(value):
    u = urlsplit(value.strip())
    if u.scheme not in ('https', 'http') or not u.netloc or u.username or u.password:
        raise ValueError('本次目标须为已确认的网页文档 URL')
    return urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path or '/', '', ''))


def node_id_from_url(value):
    parts=[unquote(part) for part in urlsplit(canonical(value)).path.split('/') if part]
    for index,part in enumerate(parts[:-1]):
        if part=='nodes' and parts[index+1].strip():
            return parts[index+1]
    return None


def bounds(value):
    match = re.fullmatch(r'([A-Z]+)([1-9]\d*):([A-Z]+)([1-9]\d*)', value)
    if not match:
        raise ValueError('范围须为完整矩形，例如 A1:AQ198')
    def col(letters):
        result = 0
        for letter in letters:
            result = result * 26 + ord(letter) - 64
        return result - 1
    left, top, right, bottom = col(match[1]), int(match[2])-1, col(match[3]), int(match[4])-1
    if right < left or bottom < top:
        raise ValueError('选区范围倒置')
    return {'row': top, 'col': left, 'rows': bottom-top+1, 'cols': right-left+1}


def path_value(value, base):
    path = Path(value)
    return str((base / path).resolve() if not path.is_absolute() else path.resolve())


def request(path):
    path = Path(path).resolve()
    value = read_json(path)
    value['target_url'] = canonical(value['target_url'])
    url_node_id=node_id_from_url(value['target_url'])
    if value.get('node_id') and value['node_id']!=url_node_id:
        raise ValueError('请求node_id必须与目标URL中的真实节点一致')
    if url_node_id:
        value['node_id']=url_node_id
    if not value.get('sheets') or len({s['name'] for s in value['sheets']}) != len(value['sheets']):
        raise ValueError('须指定非空且不重复的工作表名')
    for sheet in value['sheets']:
        if not isinstance(sheet['name'], str) or not sheet['name'].strip():
            raise ValueError('工作表名须为字符串')
        if sheet.get('range'):
            sheet['bounds'] = bounds(sheet['range'])
            sheet['discover'] = False
        else:
            sheet['discover'] = True
    if type(value.get('header_search_rows',20)) is not int or value.get('header_search_rows',20) < 1:
        raise ValueError('表头搜索行数须为正整数')
    for name in ('source', 'state'):
        if value.get(name):
            value[name] = path_value(value[name], path.parent)
    return value


def safe_outputs(inputs, outputs):
    protected = {Path(p).resolve() for p in inputs if p}
    wanted = [Path(p).resolve() for p in outputs]
    if protected.intersection(wanted) or len(wanted) != len(set(wanted)):
        raise ValueError('输出不得覆盖源文件、快照、请求、状态或其他输出')
    if any(p.is_relative_to(HERE.parent) for p in wanted):
        raise ValueError('运行数据不得写入 skill 安装目录')


def browser_code(spec, path, transport='json'):
    factory = (HERE / 'dingtalk_batch.js').read_text(encoding='utf-8')
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # All business values are JSON data, never interpolated as executable code.
    worker = '(' + factory.rstrip() + ')(' + json.dumps(spec, ensure_ascii=True) + ')'
    code = worker + '\n'
    if transport == 'gzip':
        code = '''async (page) => {
  const worker = ''' + worker + ''';
  const data = await worker(page);
  try {
    return await page.evaluate(async value => {
      if (typeof CompressionStream !== 'function') return value;
      const raw = JSON.stringify(value);
      const stream = new Blob([raw]).stream().pipeThrough(new CompressionStream('gzip'));
      const bytes = new Uint8Array(await new Response(stream).arrayBuffer());
      let binary = '';
      for (let i=0; i<bytes.length; i+=16384) binary += String.fromCharCode(...bytes.subarray(i,i+16384));
      return {packet_version:1, transport:'gzip-base64', payload:btoa(binary),
        utf8_bytes:new TextEncoder().encode(raw).length,
        summary:{schema_version:value.schema_version, batch_id:value.batch_id, phase:value.phase,
          ok:value.ok, target_url:value.target_url, failure:value.failure,
          sheets:value.sheets.map(s=>({name:s.name, range:s.range}))}};
    }, data);
  } catch (error) {
    // Compression failure must never cause a repeated copy or a lost partial receipt.
    return {...data, transport_warning:'gzip_unavailable'};
  }
}\n'''
    Path(path).write_text(code, encoding='utf-8')


def collect_code(args):
    req = request(args.request)
    safe_outputs([args.request, req.get('source'), req.get('state')], [args.out])
    spec = {'schema_version': 1, 'phase': 'collect', 'kind': KIND, 'batch_id': uuid.uuid4().hex,
            'target_url': req['target_url'], 'sheets': req['sheets'], 'header_search_rows':req.get('header_search_rows',20)}
    transport = getattr(args,'transport','gzip')
    browser_code(spec, args.out, transport)
    return {'code_file': str(Path(args.out).resolve()), 'phase': 'collect', 'sheets': [s['name'] for s in req['sheets']],
            'transport':transport,'bridge_file':str(HERE/'batch_bridge.js')}


def snapshot_rows(req, snapshot):
    if snapshot.get('transport') == 'dingtalk_sheet':
        return dingtalk_sheet_snapshot_rows(req, snapshot)
    if snapshot.get('transport') == 'dingtalk_mcp':
        return mcp_snapshot_rows(req, snapshot)
    if snapshot.get('phase') != 'collect' or not snapshot.get('ok') or canonical(snapshot['target_url']) != req['target_url']:
        raise ValueError('采集快照未成功或不属于本次文档')
    collected = snapshot.get('sheets', [])
    if [s.get('name') for s in collected] != [s['name'] for s in req['sheets']]:
        raise ValueError('快照工作表范围与请求不同')
    result = []
    for item, sheet in zip(collected, req['sheets']):
        discovered = sheet.get('discover',False)
        if discovered:
            if item.get('copy_scope') != 'whole_sheet':
                raise ValueError('自动范围须来自本轮整表复制')
            sheet['range'] = item['range']
            sheet['bounds'] = bounds(item['range'])
        box, origin = sheet['bounds'], item.get('from', {})
        if item['range'] != sheet['range'] or not item.get('sheet_id') or origin.get('sheetId') != item['sheet_id']:
            raise ValueError('复制工作表标识不完整')
        if discovered:
            projection = item.get('projection',{})
            copied = projection.get('copied_columns')
            if (box['row'] != 0 or box['col'] != 0 or origin.get('row') != 0 or origin.get('col') != 0
                or projection.get('copied_rows') != box['rows'] or type(copied) is not int or copied < box['cols']
                or projection.get('kept_columns') != box['cols']
                or projection.get('removed_trailing_blank_columns') != copied-box['cols']):
                raise ValueError('整表列裁剪元数据不完整')
            if (origin.get('rowCount'),origin.get('colCount')) not in ((0,0),(box['rows'],copied)):
                raise ValueError('整表复制行列数与元数据不符')
        elif any(origin.get(k) != box[v] for k, v in [('row','row'),('col','col'),('rowCount','rows'),('colCount','cols')]):
            raise ValueError('快照矩形与请求不同')
        rows = list(csv.reader(io.StringIO(item['tsv'], newline=''), delimiter='\t'))
        if len(rows) != box['rows'] or any(len(row) != box['cols'] for row in rows):
            raise ValueError('快照行列数不完整')
        result.append([[value.replace('\r\n','\n').replace('\r','\n') for value in row] for row in rows])
    return result


def dingtalk_sheet_snapshot_rows(req, snapshot):
    """Validate a complete ordinary-sheet snapshot while preserving its real A1 scope."""
    node_id = snapshot.get('node_id')
    if (snapshot.get('schema_version') != 1 or snapshot.get('phase') != 'collect'
        or snapshot.get('ok') is not True or snapshot.get('complete') is not True
        or not isinstance(node_id,str) or not node_id.strip()
        or not req.get('node_id') or node_id != req['node_id']
        or canonical(snapshot.get('target_url','')) != req['target_url']):
        raise ValueError('普通钉钉表格快照未成功或文档身份不符')
    collected = snapshot.get('sheets')
    if not isinstance(collected,list) or [s.get('name') for s in collected] != [s['name'] for s in req['sheets']]:
        raise ValueError('普通钉钉表格工作表范围与请求不同')
    sheet_ids=set(); result=[]
    for item,sheet in zip(collected,req['sheets']):
        meta=item.get('sheet_mcp')
        sheet_id=item.get('sheet_id')
        if (not isinstance(sheet_id,str) or not sheet_id.strip() or sheet_id in sheet_ids
            or not isinstance(meta,dict) or set(meta)!={'node_id','sheet_id','row_indices','col_indices','has_more'}
            or meta.get('node_id')!=node_id or meta.get('sheet_id')!=sheet_id or meta.get('has_more') is not False
            or item.get('scope_complete') is not True):
            raise ValueError('普通钉钉表格工作表身份或完整性标记不符')
        sheet_ids.add(sheet_id)
        value=item.get('range')
        box=bounds(value) if isinstance(value,str) else None
        if box is None or box['row']!=0 or box['col']!=0:
            raise ValueError('普通钉钉表格范围须从A1开始的完整矩形')
        if sheet.get('range') and sheet['range']!=value:
            raise ValueError('本次请求范围与普通钉钉表格快照不同')
        tsv=item.get('tsv')
        if not isinstance(tsv,str):
            raise ValueError('普通钉钉表格快照缺少TSV')
        rows=list(csv.reader(io.StringIO(tsv,newline=''),delimiter='\t',strict=True))
        if len(rows)!=box['rows'] or not rows or any(len(row)!=box['cols'] for row in rows):
            raise ValueError('普通钉钉表格范围与TSV行列不完整')
        headers=[value.strip() for value in rows[0]]
        if any(not value for value in headers) or len(set(headers))!=len(headers):
            raise ValueError('普通钉钉表格表头为空或重复')
        expected_rows=list(range(1,box['rows']+1))
        def column_name(n):
            out=''
            while n:
                n,rem=divmod(n-1,26); out=chr(65+rem)+out
            return out
        expected_cols=[column_name(n) for n in range(1,box['cols']+1)]
        row_indices=meta.get('row_indices'); col_indices=meta.get('col_indices')
        if (not isinstance(row_indices,list) or any(type(value) is not int for value in row_indices)
            or row_indices!=expected_rows or col_indices!=expected_cols):
            raise ValueError('普通钉钉表格返回行列坐标不连续或与A1矩形不符')
        sheet['range']=value; sheet['bounds']=box; sheet['discover']=False
        result.append([[cell.replace('\r\n','\n').replace('\r','\n') for cell in row] for row in rows])
    return result


def mcp_snapshot_rows(req, snapshot):
    """Validate a complete dingtalk_aitable snapshot and adapt it to the local TSV planner."""
    base_id = snapshot.get('base_id')
    if (snapshot.get('schema_version') != 1 or snapshot.get('phase') != 'collect'
        or snapshot.get('ok') is not True or snapshot.get('complete') is not True
        or not isinstance(base_id, str) or not base_id.strip()
        or canonical(snapshot.get('target_url', '')) != req['target_url']):
        raise ValueError('MCP采集快照未完整成功或身份不符')
    collected = snapshot.get('sheets')
    if not isinstance(collected, list) or [s.get('name') for s in collected] != [s['name'] for s in req['sheets']]:
        raise ValueError('MCP快照工作表范围与请求不同')
    result = []
    for item, sheet in zip(collected, req['sheets']):
        meta = item.get('mcp')
        if (item.get('scope_complete') is not True or 'next_cursor' not in item or item.get('next_cursor') is not None
            or not isinstance(meta, dict) or meta.get('base_id') != base_id
            or not isinstance(meta.get('table_id'), str) or not meta['table_id'].strip()
            or item.get('sheet_id') != meta['table_id']):
            raise ValueError('MCP表快照未完整、分页未结束或表身份不符')
        fields = meta.get('fields')
        record_ids = meta.get('record_ids')
        if not isinstance(fields, list) or not isinstance(record_ids, list):
            raise ValueError('MCP快照缺少字段或记录ID清单')
        field_ids, field_names = [], []
        for field in fields:
            if not isinstance(field, dict) or not all(isinstance(field.get(k), str) and field[k].strip() for k in ('field_id','name','type')):
                raise ValueError('MCP字段缺少非空field_id/name/type')
            field_ids.append(field['field_id']); field_names.append(field['name'])
        if not fields or len(set(field_ids)) != len(field_ids) or len(set(field_names)) != len(field_names):
            raise ValueError('MCP field_id或字段名为空、重复')
        if (any(not isinstance(x, str) or not x.strip() for x in record_ids)
            or len(set(record_ids)) != len(record_ids)):
            raise ValueError('MCP record_id为空或重复')
        tsv = item.get('tsv')
        if not isinstance(tsv, str):
            raise ValueError('MCP快照缺少TSV矩阵')
        rows = list(csv.reader(io.StringIO(tsv, newline=''), delimiter='\t', strict=True))
        if (not rows or rows[0] != field_names or len(rows) != len(record_ids)+1
            or any(len(row) != len(fields) for row in rows)):
            raise ValueError('MCP字段表头、记录数或列数与元数据不符')
        # MCP supplies logical table data, not browser coordinates. Give the local
        # matching engine a virtual A1 rectangle without fabricating `from` data.
        def column_name(n):
            out = ''
            while n:
                n, rem = divmod(n-1, 26)
                out = chr(65+rem) + out
            return out
        virtual_range = f"A1:{column_name(len(fields))}{len(rows)}"
        if item.get('range') != virtual_range:
            raise ValueError('MCP虚拟范围与字段/记录矩阵不符')
        sheet['range'] = virtual_range
        sheet['bounds'] = bounds(virtual_range)
        sheet['discover'] = False
        result.append([[value.replace('\r\n','\n').replace('\r','\n') for value in row] for row in rows])
    return result


def audit_plan(module, plan, kind):
    """Check plan invariants before creating an executable submission artifact."""
    before, expected = plan['before_matrix'], plan['expected_matrix']
    if len(before) != len(expected) or any(len(a) != len(b) for a, b in zip(before, expected)):
        raise ValueError('计划改变了表格范围')
    if kind == 'workorder':
        col = plan['target']['workorder_column_index']
        ro = plan['target']['row_offset']
        changed = {(c['row']-ro-1, col):c for c in plan['changes']}
        by_row = {item['row']:item for item in plan['rows']}
        for item in plan['rows']:
            if item['status'] in ('single_candidate', 'all_candidates_in_one_cell'):
                if module.stored_orders(item['after']) != item['candidates']:
                    raise ValueError('单行未包含全部工单候选')
        for group in plan['duplicate_groups']:
            items = [by_row[r] for r in group['rows']]
            if all(i['status'] == 'distinct_assignment' for i in items):
                assigned = [i['after'] for i in items]
                if len(set(assigned)) != len(assigned) or not set(assigned).issubset(group['candidates']):
                    raise ValueError('重复组分配错误')
    else:
        changed = {(c['matrix_row'], c['column']):c for c in plan['changes']}
        for item in plan['rows']:
            for field, value in item['requests'].items():
                if not set(module.ids(value['before'])).issubset(module.ids(value['after'])):
                    raise ValueError('号码计划丢失旧号码')
                if not plan['only_empty'] and not set(value['candidates']).issubset(module.ids(value['after'])):
                    raise ValueError('号码计划未包含全部候选')
        if not plan['only_empty'] and not plan['completeness']['all_candidate_numbers_in_result']:
            raise ValueError('领料候选完整性检查失败')
    if len(changed) != len(plan['changes']):
        raise ValueError('计划包含重复修改单元格')
    for r, (old, new) in enumerate(zip(before, expected)):
        for c, (a, b) in enumerate(zip(old, new)):
            change = changed.get((r,c))
            if (a != b) != bool(change) or (change and (change['before'] != a or change['after'] != b)):
                raise ValueError('逐格变更清单与完整结果不一致')


def prepare(args):
    req = request(args.request)
    if not req.get('source'):
        raise ValueError('请求缺少源文件 source')
    snapshot = read_json(args.snapshot)
    targets = snapshot_rows(req, snapshot)
    directory = Path(args.out_dir).resolve()
    if (directory / 'batch.json').exists():
        raise ValueError('此运行目录已有计划，请为新一轮使用新目录')
    state_path = req.get('state') or str(directory / 'picking-state.json')
    outputs = [directory/'batch.json', directory/'summary.json', directory/'changes.csv', directory/'exceptions.csv']
    outputs += [directory/f'plan-{i+1}.json' for i in range(len(targets))]
    safe_outputs([args.request, args.snapshot, req['source'], req.get('state')], outputs)
    module = engine()
    source = Path(req['source'])
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    # One XLSX load per run, regardless of the number of target sheets.
    sources = module.read_tables(source, req.get('source_sheets'))
    if hashlib.sha256(source.read_bytes()).hexdigest() != fingerprint:
        raise ValueError('读取期间源文件改变，须重新读取')
    prepared_index = module.prepare_source_index(sources, req.get('header_search_rows',20), bool(req.get('source_sheets')))
    state = module.load_state(state_path, req['target_url']) if KIND == 'picking' else None
    batch = {'schema_version': 1, 'batch_id': uuid.uuid4().hex, 'kind': KIND, 'target_url': req['target_url'],
             'source': str(source), 'source_sha256': fingerprint, 'state': state_path if KIND == 'picking' else None,
             'request_file': str(Path(args.request).resolve()), 'snapshot_file': str(Path(args.snapshot).resolve()),
             'transport': snapshot.get('transport', 'browser'), 'sheets': []}
    if snapshot.get('transport') in ('dingtalk_mcp','dingtalk_sheet'):
        if snapshot.get('transport') == 'dingtalk_sheet':
            batch['node_id'] = snapshot['node_id']
        else:
            batch['base_id'] = snapshot['base_id']
    plans = []
    for index, (sheet, original, snap) in enumerate(zip(req['sheets'], targets, snapshot['sheets'])):
        box = sheet['bounds']; rows = [r[:] for r in original]
        if KIND == 'workorder':
            actual_col = sheet.get('workorder_column', '工单单号')
            fields = (*module.TARGET_FIELDS[:4], actual_col)
            found = module.find_header(rows, fields, req.get('header_search_rows',20))
            if not found:
                raise ValueError('工作表缺少本次指定的工单表头：' + sheet['name'])
            head, cols = found
            if actual_col != '工单单号' and '工单单号' in [module.identifier(v) for v in rows[head]]:
                raise ValueError('工单列别名与已有同名列冲突')
            rows[head][cols[actual_col]] = '工单单号'
            plan = module.build_plan(sources, rows, req.get('header_search_rows',20), box['row'], box['col'], bool(req.get('source_sheets')), prepared_index)
            if req.get('only_empty') and any(module.identifier(c['before']) for c in plan['changes']):
                raise ValueError('只填空白请求的计划涉及已有工单，请使用原助手核实相关组')
            plan['target']['sheet'] = sheet['name']
            guard = {'before': original, 'columns': list(cols.values())}
            changes = [{'r': c['row']-box['row']-1, 'c': cols[actual_col], 'field': actual_col, **c} for c in plan['changes']]
        else:
            plan = module.build_plan(sources, rows, sheet['name'], req['target_url'], state,
                sheet.get('workorder_column','包装工单'), req.get('header_search_rows',20), box['row'], box['col'],
                req.get('only_empty',False), bool(req.get('source_sheets')), prepared_index)
            guard = {'before': original, 'header': plan['target']['header_index'],
                     'order_column': plan['target']['columns'][plan['target']['workorder_column']],
                     'orders': [{'r':i['matrix_row'],'orders':i['workorders']} for i in plan['rows']]}
            changes = [{'r':c['matrix_row'],'c':c['column'], **c} for c in plan['changes']]
            plan['state_file'] = state_path
        audit_plan(module, plan, KIND)
        plan.update(source_file=str(source), target_file=str(Path(args.snapshot).resolve()))
        for c in changes:
            c['row'] = c['r'] + box['row']  # zero-based absolute coordinates for browser batching
            c['col'] = c['c'] + box['col']
        plan_path = directory/f'plan-{index+1}.json'
        plans.append((plan_path, plan))
        entry = {'name':sheet['name'], 'range':sheet['range'], 'bounds':box, 'sheet_id':snap['sheet_id'],
                 'plan_file':str(plan_path), 'guard':guard, 'changes':changes}
        if snapshot.get('transport') == 'dingtalk_mcp':
            entry['mcp'] = snap['mcp']
        elif snapshot.get('transport') == 'dingtalk_sheet':
            entry['sheet_mcp'] = snap['sheet_mcp']
        batch['sheets'].append(entry)
    for path, plan in plans:
        save_json(path,plan)
    save_json(directory/'batch.json',batch)
    result = report_data(batch, None, directory, module)
    return {'batch_file':str(directory/'batch.json'), 'audit_passed':True, 'source_loads':1,
            'source_index_builds':1, **result}


def submission_spec(batch, output_path, batch_path=None):
    if batch.get('kind') != KIND or batch.get('schema_version') != 1:
        raise ValueError('运行计划类型或版本不符')
    if hashlib.sha256(Path(batch['source']).read_bytes()).hexdigest() != batch['source_sha256']:
        raise ValueError('源文件已变化，须重新生成完整计划')
    safe_outputs([batch_path,batch['source'],batch.get('state'),batch['request_file'],batch['snapshot_file']]
                 + [s['plan_file'] for s in batch['sheets']], [output_path])
    spec = {**batch, 'phase':'submit', 'skipped_history':[]}
    if KIND == 'picking':
        module = engine()
        state = module.load_state(batch['state'],batch['target_url'])
        for sheet in spec['sheets']:
            skip = module.blocked(state,sheet['name'])
            allowed = []
            for c in sheet['changes']:
                if c['field'] == module.MARKER and set(c['finished_workorders']).issubset(skip):
                    spec['skipped_history'].append({'sheet':sheet['name'],'cell':c['cell']})
                else:
                    allowed.append(c)
            sheet['changes'] = allowed
    return spec


def submit_code(args):
    batch = read_json(args.batch)
    if batch.get('transport') in ('dingtalk_mcp','dingtalk_sheet'):
        raise ValueError('MCP计划不得生成浏览器提交代码；请用submit-spec输出本地提交规范')
    spec = submission_spec(batch,args.out,args.batch)
    browser_code(spec,args.out)
    return {'code_file':str(Path(args.out).resolve()),'phase':'submit','batch_id':batch['batch_id'],
            'planned_cells':sum(len(s['changes']) for s in spec['sheets']), 'history_skipped':len(spec['skipped_history'])}


def submit_spec(args):
    batch = read_json(args.batch)
    if batch.get('transport') == 'dingtalk_sheet':
        if not batch.get('node_id'):
            raise ValueError('普通钉钉表格计划缺少node_id')
        raw = submission_spec(batch,args.out,args.batch)
        sheets=[]
        for sheet in raw['sheets']:
            changes=[]
            for change in sheet['changes']:
                cell=change.get('cell')
                if not isinstance(cell,str) or not re.fullmatch(r'[A-Z]+[1-9]\d*',cell):
                    raise ValueError('计划变更缺少真实A1单元格坐标')
                changes.append({'cell':cell,'field':change['field'],'before':change['before'],
                                'after':change['after'],'value':change['after']})
            bounds_value=sheet['bounds']
            sheets.append({'name':sheet['name'],'node_id':raw['node_id'],'sheet_id':sheet['sheet_id'],
                           'range':sheet['range'],'sheet_mcp':sheet['sheet_mcp'],
                           'required_preflight':{'full_range':sheet['range'],'rows':bounds_value['rows'],
                                                 'columns':bounds_value['cols'],'identity_check_required':True},
                           'changes':changes})
        result={'schema_version':1,'transport':'dingtalk_sheet','phase':'submit','batch_id':raw['batch_id'],
                'kind':raw['kind'],'target_url':raw['target_url'],'node_id':raw['node_id'],
                'preflight_status':'required_not_performed','skipped_history':raw['skipped_history'],'sheets':sheets}
        save_json(args.out,result)
        return {'spec_file':str(Path(args.out).resolve()),'phase':'submit','transport':'dingtalk_sheet',
                'batch_id':batch['batch_id'],'planned_cells':sum(len(s['changes']) for s in sheets),
                'history_skipped':len(raw['skipped_history']),'preflight_performed':False}
    if batch.get('transport') != 'dingtalk_mcp' or not batch.get('base_id'):
        raise ValueError('submit-spec仅适用于dingtalk_mcp或dingtalk_sheet计划')
    spec = submission_spec(batch,args.out,args.batch)
    save_json(args.out,spec)
    return {'spec_file':str(Path(args.out).resolve()),'phase':'submit','transport':'dingtalk_mcp',
            'batch_id':batch['batch_id'],'planned_cells':sum(len(s['changes']) for s in spec['sheets']),
            'history_skipped':len(spec['skipped_history']), 'online_preflight_performed':False}


def check_before(args):
    batch=read_json(args.batch); snapshot=read_json(args.snapshot)
    if batch.get('kind')!=KIND or batch.get('transport')!='dingtalk_sheet' or batch.get('schema_version')!=1:
        raise ValueError('check-before仅适用于dingtalk_sheet计划')
    req=request(batch['request_file'])
    if batch.get('node_id')!=req.get('node_id') or canonical(batch['target_url'])!=req['target_url']:
        raise ValueError('计划与请求的文档node_id/URL不符')
    current=snapshot_rows(req,snapshot)
    if snapshot.get('node_id')!=batch['node_id'] or canonical(snapshot['target_url'])!=batch['target_url']:
        raise ValueError('写前快照的node_id/URL与计划不符')
    if len(snapshot['sheets'])!=len(batch['sheets']):
        raise ValueError('写前快照工作表数量与计划不同')
    module=engine(); reports=[]; all_ok=True
    for snap, data, entry in zip(snapshot['sheets'],current,batch['sheets']):
        meta=snap.get('sheet_mcp',{})
        same_identity=(snap.get('name')==entry['name'] and snap.get('sheet_id')==entry['sheet_id']
                       and snap.get('range')==entry['range'] and meta==entry.get('sheet_mcp'))
        plan=read_json(entry['plan_file'])
        if not same_identity:
            result={'ok':False,'reason':'sheet_node_or_scope_identity_changed','difference_count':1,'differences':[]}
        elif KIND=='workorder':
            original_before=entry.get('guard',{}).get('before')
            if not isinstance(original_before,list):
                raise ValueError('普通表写前检查缺少真实表头的原始保护矩阵')
            count,sample=module.matching_differences(original_before,data,plan)
            result={'ok':count==0,'reason':None if count==0 else 'protected_matching_fields_changed',
                    'difference_count':count,'differences':sample}
        else:
            state=module.load_state(batch['state'],batch['target_url'])
            checked=module.check(plan,data,state,before=True)
            result={'ok':checked['ok'],'reason':None if checked['ok'] else 'protected_picking_fields_changed',
                    'difference_count':len(checked['differences']),'differences':checked['differences'],
                    'accepted_updates':checked['accepted_updates']}
        all_ok=all_ok and result['ok']
        reports.append({'sheet':entry['name'],'node_id':batch['node_id'],'sheet_id':entry['sheet_id'],
                        'range':entry['range'],'rows':entry['bounds']['rows'],'columns':entry['bounds']['cols'],**result})
    basis=('local_receipt_projection' if snapshot.get('snapshot_basis')=='local_receipt_projection'
           else 'snapshot_reuse' if Path(args.snapshot).resolve()==Path(batch['snapshot_file']).resolve()
           else 'snapshot_comparison')
    report={'ok':all_ok,'transport':'dingtalk_sheet','phase':'check-before','batch_id':batch['batch_id'],
            'node_id':batch['node_id'],'target_url':batch['target_url'],'sheets':reports,
            'check_basis':basis,'online_read_performed':False,
            'snapshot_file':str(Path(args.snapshot).resolve()),
            'snapshot_sha256':hashlib.sha256(Path(args.snapshot).read_bytes()).hexdigest()}
    if args.out:
        safe_outputs([args.batch,args.snapshot,batch['source'],batch.get('state'),batch['request_file'],batch['snapshot_file']]
                     +[s['plan_file'] for s in batch['sheets']],[args.out])
        save_json(args.out,report)
    return report


def receipt_cells(batch, receipt):
    if receipt is None:
        return set(),set(),set()
    if receipt.get('batch_id') != batch['batch_id'] or receipt.get('phase') != 'submit' or receipt.get('kind') != batch['kind'] or canonical(receipt['target_url']) != batch['target_url']:
        raise ValueError('提交回执不属于本轮完整计划')
    known = {(s['name'],c['cell']) for s in batch['sheets'] for c in s['changes']}
    submitted = [(entry['sheet'],cell) for entry in receipt.get('submitted',[]) for cell in entry['cells']]
    present = [(entry['sheet'],entry['cell']) for entry in receipt.get('already_present',[])]
    skipped = [(entry['sheet'],entry['cell']) for entry in receipt.get('skipped_history',[])]
    all_cells = submitted+present+skipped
    if len(set(all_cells)) != len(all_cells) or not set(all_cells).issubset(known):
        raise ValueError('回执包含重复或计划以外的单元格')
    if receipt.get('ok') and set(all_cells) != known:
        raise ValueError('成功回执未覆盖完整计划')
    return set(submitted),set(present),set(skipped)


def report_data(batch, receipt, directory, module):
    directory = Path(directory)
    submitted,present,skipped = receipt_cells(batch,receipt)
    outputs = [directory/'summary.json',directory/'changes.csv',directory/'exceptions.csv']
    safe_outputs([batch['source'],batch.get('state'),batch['request_file'],batch['snapshot_file']]
                 + [s['plan_file'] for s in batch['sheets']], outputs)
    rows, exceptions, stats = [],[],{}
    for sheet in batch['sheets']:
        plan = read_json(sheet['plan_file']); stats[sheet['name']] = plan['statistics']
        for change in sheet['changes']:
            key = (sheet['name'],change['cell'])
            status = '已提交，未做写后复核' if key in submitted else '写前已为完整结果' if key in present else '已有处理历史，保留现状' if key in skipped else '待提交' if receipt is None else '未提交或状态待确认'
            added = change.get('added')
            if added is None:
                added = sorted(set(module.stored_orders(change['after']))-set(module.stored_orders(change['before']))) if KIND == 'workorder' else []
            orders = change.get('workorders',change.get('candidates',[]))
            rows.append([sheet['name'],change['cell'],change['field'],'/'.join(orders),change['before'],change['after'],'/'.join(added),status])
        if KIND == 'workorder':
            exceptions.extend([sheet['name'],'/'.join(map(str,i['rows'])),i['reason'],json.dumps(i,ensure_ascii=False)] for i in plan['issues'])
        else:
            for item in plan['rows']:
                if not item['workorders']:
                    exceptions.append([sheet['name'],item['row'],'包装工单为空，保留现状',''])
                elif not any(v['candidates'] for v in item['requests'].values()):
                    exceptions.append([sheet['name'],item['row'],'源文件无可分类领料号码，保留现状','/'.join(item['workorders'])])
            exceptions.extend([sheet['name'],i['row'],'原有号码保留或只填空白限制',json.dumps(i,ensure_ascii=False)] for i in plan['audit'])
    summary = {'schema_version':1,'batch_id':batch['batch_id'],'kind':batch['kind'],'sheets':stats,
               'planned_cells':len(rows),'submitted_cells':len(submitted),'already_present_cells':len(present),
               'history_skipped_cells':len(skipped),'exception_records':len(exceptions),'persistence_verified':False,
               'submission_ok':receipt.get('ok') if receipt else None,'failure':receipt.get('failure') if receipt else None,
               'basis':'完整写前计划及实际提交反馈；未做写后读回或刷新复核',
               'source_statistics':read_json(batch['sheets'][0]['plan_file'])['source_statistics']}
    directory.mkdir(parents=True,exist_ok=True)
    for path,header,values in [(outputs[1],['工作表','单元格','字段','匹配工单/候选','原值','计划完整结果','本次新增号码','提交状态'],rows),
                               (outputs[2],['工作表','行号','原因','明细'],exceptions)]:
        with path.open('w',encoding='utf-8-sig',newline='') as stream:
            writer = csv.writer(stream); writer.writerow(header); writer.writerows(values)
    save_json(outputs[0],summary)
    return {'summary_file':str(outputs[0]),'changes_file':str(outputs[1]),'exceptions_file':str(outputs[2]),
            'planned_cells':len(rows),'submitted_cells':len(submitted),'exception_records':len(exceptions),
            'sheets':stats,'submission_ok':summary['submission_ok']}


def report(args):
    batch = read_json(args.batch)
    if batch.get('kind') != KIND:
        raise ValueError('计划与本技能类型不符')
    receipt = read_json(args.receipt)
    submitted,_,_ = receipt_cells(batch,receipt)
    module = engine()
    # Validate output paths and generate the report before updating local history.
    safe_outputs([args.batch,args.receipt], [Path(args.out_dir)/name for name in ('summary.json','changes.csv','exceptions.csv')])
    result = report_data(batch,receipt,Path(args.out_dir).resolve(),module)
    if KIND == 'picking':
        state = module.load_state(batch['state'],batch['target_url'])
        changed = False
        for sheet in batch['sheets']:
            plan = read_json(sheet['plan_file'])
            marker_rows = [c['row'] for c in plan['changes'] if c['field'] == module.MARKER and (sheet['name'],c['cell']) in submitted]
            if marker_rows:
                state = module.record_submitted(plan,state,marker_rows)
                changed = True
        safe_outputs([args.batch,args.receipt,batch['source'],batch['request_file'],batch['snapshot_file']]
                     + [s['plan_file'] for s in batch['sheets']], [batch['state']])
        if changed:
            save_json(batch['state'],state)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    collect = sub.add_parser('collect-code',help='生成一次读取全部指定工作表的浏览器脚本')
    collect.add_argument('--request',required=True); collect.add_argument('--out',required=True)
    collect.add_argument('--transport',choices=('gzip','json'),default='gzip')
    prepare_parser = sub.add_parser('prepare',help='只读一次源文件，生成全部计划、写前检查参数和初始报告')
    prepare_parser.add_argument('--request',required=True); prepare_parser.add_argument('--snapshot',required=True); prepare_parser.add_argument('--out-dir',required=True)
    submit = sub.add_parser('submit-code',help='核实源文件及最新提示历史，生成一次批量写前检查与提交脚本')
    submit.add_argument('--batch',required=True); submit.add_argument('--out',required=True)
    submit_spec_parser = sub.add_parser('submit-spec',help='为MCP适配器生成本地提交规范，不连接或写入在线表格')
    submit_spec_parser.add_argument('--batch',required=True); submit_spec_parser.add_argument('--out',required=True)
    check_parser = sub.add_parser('check-before',help='核对普通钉钉表格写前完整范围身份与受保护字段')
    check_parser.add_argument('--batch',required=True); check_parser.add_argument('--snapshot',required=True); check_parser.add_argument('--out')
    report_parser = sub.add_parser('report',help='根据提交回执生成报告，并登记实际已提交提示')
    report_parser.add_argument('--batch',required=True); report_parser.add_argument('--receipt',required=True); report_parser.add_argument('--out-dir',required=True)
    args = parser.parse_args(argv)
    try:
        command = {'collect-code':collect_code,'prepare':prepare,'submit-code':submit_code,
                   'submit-spec':submit_spec,'check-before':check_before,'report':report}[args.command]
        print(json.dumps(command(args),ensure_ascii=False))
        return 0
    except (ValueError,OSError,KeyError,TypeError,csv.Error) as error:
        print('错误：'+str(error),file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
