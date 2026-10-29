#!/usr/bin/env python3
"""One ERP writer: open both workorder branches, then collect one approval set.

Local branch plans may be produced concurrently. All business calls use the
parent mcp_session transport sequentially; this module never logs in itself.
"""
import hashlib
import json
import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import re
import time
import uuid

from order_workorders import Workflow, request_config, validate_generated, validate_plan, quantity, selected_source_orders
from manual_workorders import ManualRun, check_source, snapshot_rows, same_document, validate_source_check
from workorder_sources import is_manual_source


def need(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+'.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temp.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local(base, name):
    result = (base/name).resolve()
    need(result != base and result.is_relative_to(base), '路径须位于本轮工作目录')
    return result


def documents(packet, count_key):
    need(packet.get('ok') is True and packet.get('complete') is True, '开单回执未完整成功')
    rows = packet['documents']
    need(type(packet[count_key]) is int and packet[count_key] == len(rows), '开单数量或单据清单不完整')
    ids = [r['ID'] for r in rows]; numbers = [r['DOC_NO'] for r in rows]
    need(len(ids) == len(set(ids)) and len(numbers) == len(set(numbers)), '分支单据ID或号码重复')
    need(all(str(uuid.UUID(v)) == v and v != str(uuid.UUID(int=0)) for v in ids), '工单ID无效')
    need(all(re.fullmatch(r'5101-[A-Za-z0-9]+', v) is not None for v in numbers), '工单号码无效')
    return rows


def no_manual_needed(scope, snapshot):
    """A missing branch never means done: prove the explicit zero-new-row case."""
    need(scope.get('specs') == [], '空分支必须明确列出空specs')
    excluded = {(r['sheet'], r['row']) for r in scope.get('excluded_rows', [])}
    tables = snapshot_rows(scope, snapshot)
    for sheet, (rows, head, cols) in tables.items():
        for row_index, row in enumerate(rows[head+1:], head+2):
            if (sheet,row_index) in excluded:
                continue
            row = row + [''] * max(0, max(cols.values())+1-len(row))
            source, item, name, order = (row[cols[f]] for f in ('单号','销货明细_品号','销货明细_品名','包装工单'))
            if not is_manual_source(source) or not item.startswith('1-'):
                continue
            rules = scope['selection_rules']
            if item.startswith('1-V') and any(v.casefold() in name.casefold() for v in rules['excluded_1_v_name_keywords']):
                continue
            if item.startswith('1-H') and any(v.casefold() in name.casefold() for v in rules['excluded_1_h_name_keywords']):
                continue
            need(order.strip(), '仍有手工来源成品缺少工单，不能把该分支标成零新增')


def complete_manual_scope(scope, snapshot):
    if scope['specs']:
        check_source(scope, snapshot)
    # Account for every eligible blank row, including rows accidentally omitted
    # from a nonempty plan. User exclusions remain explicit in the input.
    no_manual_needed({**scope, 'specs':[], 'excluded_rows':scope.get('excluded_rows', []) +
                      [{'sheet':s['sheet'],'row':s['row']} for s in scope['specs']]}, snapshot)


def candidate_semantics(rows):
    """Stable business meaning of selected candidates, independent of RAM IDs."""
    return Counter((r['item_code'], r['item_name'], quantity(r['plan_quantity']),
                    r['draft']['ITEM_FEATURE_ID'], tuple(sorted(r['source_ids'])),
                    tuple(sorted(r['source_doc_nos']))) for r in rows)


def validate_discard_set(records, expected_ids, allow_subset=False):
    ids = set()
    for op, request, result in records:
        need(op not in ids and request.get('name') == 'e10_discard_operation' and
             request.get('arguments', {}).get('operation_id') == op and result.get('ok') is True and
             result.get('discarded') is True and result.get('saved') is False,
             'discard请求/回执不匹配或状态不明确')
        ids.add(op)
    need(ids.issubset(set(expected_ids)) and (allow_subset or ids == set(expected_ids)),
         'discard回执包含非待处理操作或未覆盖要求的草稿')


def recovery_batch_start(checkpoint, key):
    row = checkpoint['batches'][key]
    state = row['state']
    need(state not in ('in_flight','unknown','blocked'),
         '恢复准备状态未知或被阻塞；保留证据，禁止重试：' + key)
    return state == 'prepared'


def validate_recovery_checkpoint(checkpoint, recovery_sha256, session_id):
    need(checkpoint.get('schema_version')==1 and checkpoint.get('phase')=='preparing'
         and checkpoint.get('recovery_sha256')==recovery_sha256
         and checkpoint.get('session_id')==session_id,
         '恢复已完成、会话已变化或checkpoint身份不符；禁止重放')
    need(all(row.get('state') not in ('in_flight','unknown','blocked') for row in checkpoint.get('batches',{}).values()),
         'checkpoint含未知/阻塞准备；保留原始证据并停止')
    return True


def record_recovery_prepare(checkpoint, key, packet, packet_path, session_id, returned_epoch=None):
    row=checkpoint['batches'][key]
    need(row.get('state')=='in_flight' and Path(packet_path).is_file(), '准备回执没有对应持久化意图/文件')
    returned_utc=datetime.now(timezone.utc).isoformat(); returned_epoch=returned_epoch or time.time()
    receipt={'packet_path':str(Path(packet_path).resolve()),'packet_sha256':digest(packet_path),
             'packet':packet,'returned_utc':returned_utc,'returned_epoch':returned_epoch,
             'session_id':session_id,'operation_id':packet.get('operation_id')}
    row['attempts'][-1].update(receipt)
    row.update(receipt)
    if packet.get('ok') is True and packet.get('saved') is False and packet.get('operation_id'):
        row['operation_id']=packet['operation_id']; row['state']='prepared'
    elif packet.get('ok') is False and not packet.get('operation_id'):
        row['state']='rejected'
    else:
        row['state']='unknown'
    return row['state']


def validate_failed_actual_result(source_ids, result, existing_ids):
    rows=result.get('rows',[])
    need(result.get('ok') is True and result.get('has_next_page') is False
         and sorted(result.get('source_ids',[]))==sorted(source_ids)
         and all(row.get('ID') in set(existing_ids) for row in rows),
         '失败操作实际源关联不完整或发现新增工单')
    return rows


def build_recovery_manifest(base, run_dir, status_path, result_path, audit_path, output_path):
    """Build the fixed local manifest from the durable branch/order state and receipts."""
    base = Path(base).resolve(); run_dir = Path(run_dir).resolve()
    output_path = Path(output_path).resolve()
    need(run_dir.is_relative_to(base) and output_path.is_relative_to(base), '恢复输入输出必须位于本轮工作目录')
    branch_state = read(run_dir/'branches-state.json')
    order_dir = run_dir/'orders'; state = read(order_dir/'state.json')
    need(branch_state.get('stage') == 'prepared' and state.get('generation',{}).get('stage') == 'prepared'
         and isinstance(state.get('in_flight'),dict), '仅支持存在明确in_flight的prepared阶段')
    entries = state['generation']['entries']; by_stem = {x['stem']:x for x in entries}
    need(len(by_stem)==len(entries), '原批次stem重复')
    active = state['in_flight']; stem = active.get('stem'); op = active.get('operation_id')
    need(stem in by_stem and by_stem[stem].get('operation_id')==op and not by_stem[stem].get('completed'),
         'in_flight未唯一对应未完成批次')
    failed_commit = order_dir/(stem+'-commit.json')
    failed_prepare = order_dir/(stem+'-prepare.json')
    commit = read(failed_commit); plan = read(failed_prepare)
    need(commit.get('ok') is False and commit.get('error_type')=='System.InvalidOperationException'
         and commit.get('error')=='订单版本或状态在准备后变化，未生成。' and commit.get('state')!='completed',
         '当前失败不是明确的版本拒绝，不能自动构造恢复清单')
    summary = plan.get('summary',{}); ids = sorted({sid for c in summary.get('selected',[]) for sid in c.get('source_ids',[])})
    status = read(status_path); actual = read(result_path)
    need(status.get('ok') is True and status.get('operation_id')==op and status.get('state')=='prepared',
         '失败操作状态未知或不是prepared')
    existing = set(summary.get('existing_workorder_ids',[]))
    rows = actual.get('rows',[])
    validate_failed_actual_result(ids,actual,existing)
    completed=[]; untouched=[]
    for entry in entries:
        path = order_dir/(entry['stem']+'-prepare.json')
        if entry.get('completed'):
            receipt = order_dir/(entry['stem']+'-commit.json')
            packet = read(path); docs = validate_generated(read(receipt),packet)
            completed.append({'stem':entry['stem'],'operation_id':entry['operation_id'],
                              'prepare':manifest_ref(base,path),'commit':manifest_ref(base,receipt)})
        elif entry['stem'] != stem and entry.get('operation_id'):
            need(not (order_dir/(entry['stem']+'-started.json')).exists()
                 and not (order_dir/(entry['stem']+'-commit.json')).exists(), '待执行批次已有started/commit，需人工核对')
            untouched.append({'stem':entry['stem'],'operation_id':entry['operation_id'],
                              'prepare':manifest_ref(base,path),
                              'started_path':manifest_rel(base,order_dir/(entry['stem']+'-started.json')),
                              'commit_path':manifest_rel(base,order_dir/(entry['stem']+'-commit.json'))})
    successful = [r for r in state['generation'].get('documents',[])]
    audit = read(audit_path); audited=audit.get('successful_documents',[])
    need(audit.get('schema_version')==1 and audit.get('success_count')==len(successful)
         and {(x['id'],x['doc_no']) for x in audited}=={(x['ID'],x['DOC_NO']) for x in successful},
         '完整成功审计与state.documents不一致')
    result = {'schema_version':1,'mode':'generation_recovery',
        'failed':{'stem':stem,'operation_id':op,'prepare':manifest_ref(base,failed_prepare),
                  'status':manifest_ref(base,status_path),'commit':manifest_ref(base,failed_commit),
                  'result':manifest_ref(base,result_path),'source_ids':ids},
        'untouched':untouched,'completed':completed,'discards':[],'audit':manifest_ref(base,audit_path)}
    save(output_path,result)
    return result


def manifest_rel(base, path):
    path=Path(path).resolve(); base=Path(base).resolve()
    need(path.is_relative_to(base) and path.is_file(), '恢复证据路径不存在或越界：'+str(path))
    return path.relative_to(base).as_posix()


def manifest_ref(base, path):
    return {'path':manifest_rel(base,path),'sha256':digest(path)}


def verified_reference(root, ref):
    need(set(ref) == {'path','sha256'}, '恢复文件引用字段不完整')
    path = (Path(root) / ref['path']).resolve()
    root = Path(root).resolve()
    need(path.is_relative_to(root) and path.is_file() and digest(path) == ref['sha256'],
         '恢复证据缺失或SHA256不符：' + str(ref['path']))
    return path, read(path)


def merge_recovered_generation(old_state, new_entries, completed_rows):
    """Keep every verified completed document and append freshly prepared batches."""
    generation = dict(old_state['generation'])
    generation['stage'] = 'prepared'
    generation['entries'] = [dict(e) for e in generation['entries']
                             if e.get('completed') or not e.get('operation_id')]
    need({r['ID'] for r in completed_rows} == {r['ID'] for r in generation['documents']},
         '已完成工单与原state.documents不符')
    generation['entries'].extend(dict(entry) for entry in new_entries)
    generation['documents'] = list(generation['documents'])
    return generation


class WorkorderBranches:
    def __init__(self, call, base, directory, config_path, session_id, emit=lambda _: None):
        self.call, self.base, self.directory = call, Path(base).resolve(), Path(directory).resolve()
        config_path = Path(config_path).resolve()
        need(self.directory != self.base and self.directory.is_relative_to(self.base), '分支目录须在本轮工作目录内')
        self.config_path, self.session_id, self.emit = config_path, session_id, emit
        config = read(config_path)
        need(set(config) == {'schema_version','order_request','manual_scope','approval'} and config['schema_version'] == 1,
             '双分支配置字段不完整或未知')
        self.order_request_path = local(self.base, config['order_request'])
        self.manual_scope_path = local(self.base, config['manual_scope'])
        raw = read(self.order_request_path)
        need('generation' in raw and ('approval' not in raw or raw['approval'] == config['approval']), '订单请求缺少生成或审核范围冲突')
        order_config = request_config({**raw,'approval':config['approval']})
        self.config = config
        self.input_hashes = {str(p):digest(p) for p in (config_path,self.order_request_path,self.manual_scope_path)}
        self.state_path = self.directory/'branches-state.json'
        need(self.state_path.exists() or not self.directory.exists() or not any(self.directory.iterdir()),
             '不能将旧的独立分支或孤立文件接入新批次')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state = read(self.state_path) if self.state_path.exists() else {'stage':'new','input_hashes':self.input_hashes}
        need(self.state['input_hashes'] == self.input_hashes, '配置/范围已变化，不能重用旧批次')
        self.order = Workflow(order_config, self.directory/'orders', call, emit)
        self.order.session = session_id
        self.manual = ManualRun(call,self.base,self.directory/'manual19',session_id,emit)
        save(self.state_path,self.state)

    def ready(self, allow_approval_failure=False):
        if allow_approval_failure:
            need(self.state.get('stage') == 'approval_collected'
                 and self.order.state.get('failure',{}).get('job') == 'approve'
                 and self.order.state.get('in_flight')
                 and (not self.state.get('failure') or self.state['failure'].get('phase') == 'approve'),
                 '仅可在统一审核失败/未知时校验原creation-gate')
        else:
            need(not self.state.get('failure') and not self.state.get('in_flight'), '有未解决的分支失败或未知状态，先核对实际单据')
        for name, expected in self.input_hashes.items():
            need(digest(Path(name)) == expected, '本轮输入已被修改')
        if self.state['stage'] != 'new':
            path = self.directory/'prepared.json'
            need(digest(path) == self.state['prepared_sha256'], '两类完整准备计划已变化')
            for name, expected in read(path)['evidence_sha256'].items():
                need(digest(Path(name)) == expected, '分支准备证据已变化')
        if allow_approval_failure:
            failure = self.order.state.pop('failure')
            inflight = self.order.state.pop('in_flight')
            try:
                self.order.ready()
            finally:
                self.order.state['failure'] = failure
                self.order.state['in_flight'] = inflight
        else:
            self.order.ready()

    def check_manual_actor(self):
        for path in self.manual.directory.glob('preview-*.json'):
            person = read(path)['current_employee']
            need(person['EMPLOYEE_NAME'] == self.config['approval']['planner_name'], '手工开单生管与统一审核人员不一致，未正式保存')

    async def resume_prepare(self, recovery_path):
        """Reconcile one interrupted serial commit and prepare only its unfinished candidates."""
        need(not self.state.get('approval_continuations'), '本轮已冻结续办清单；不再从原失败状态建立第二份')
        recovery_path = Path(recovery_path).resolve()
        need(recovery_path.is_relative_to(self.base) and recovery_path != self.base, '恢复凭据须在项目工作区内')
        rec = read(recovery_path)
        need(rec.get('schema_version') == 1 and rec.get('mode') in ('rejected_prepared_zero_created','generation_recovery') and set(rec) ==
             {'schema_version','mode','failed','untouched','completed','discards','audit'}, '恢复凭据schema不符')
        archive = self.directory/'resolved'/digest(recovery_path)[:12]
        checkpoint_path = archive/'preparation-checkpoint.json'
        resuming = archive.exists()
        checkpoint = None
        if resuming:
            need(checkpoint_path.is_file(), '恢复归档存在但checkpoint缺失；保留证据并人工核对')
            checkpoint = read(checkpoint_path)
            validate_recovery_checkpoint(checkpoint,digest(recovery_path),self.session_id)
            need(checkpoint.get('old_operations_reconciled') is True,
                 'checkpoint未完成旧operation核对/丢弃阶段')
            for operation in checkpoint.get('old_operation_evidence',{}).values():
                for ref in [operation['status'],*operation['actual'],operation['discard']]:
                    verified_reference(self.base,ref)

        def reference(ref):
            return verified_reference(recovery_path.parent, ref)

        # Recheck all original inputs and preparation evidence despite the unresolved failure.
        need(self.state.get('stage') == 'prepared' and self.state.get('input_hashes') == self.input_hashes,
             '仅支持原prepared阶段且输入哈希未变的拒绝恢复')
        for name, expected in self.input_hashes.items():
            need(digest(Path(name)) == expected, '原请求/范围输入已变化')
        branch_plan_path = self.directory/'prepared.json'
        need(digest(branch_plan_path) == self.state.get('prepared_sha256'), '原双分支prepared快照已变化')
        branch_plan = read(branch_plan_path)
        for name, expected in branch_plan['evidence_sha256'].items():
            need(digest(Path(name)) == expected, '原准备证据已变化：' + name)
        order_state_path = self.order.path('state.json')
        need(order_state_path.is_file(), '原订单状态缺失')
        old_order_state = read(order_state_path)
        entries = old_order_state.get('generation', {}).get('entries', [])
        failed = rec['failed']; untouched = rec['untouched']; completed = rec['completed']
        need(isinstance(untouched,list) and isinstance(completed,list) and isinstance(rec['discards'],list),
             '恢复操作清单格式不符')
        need(failed['stem'] == old_order_state['in_flight']['stem'] and
             failed['operation_id'] == old_order_state['in_flight']['operation_id'], 'in_flight与明确失败操作不符')
        by_stem = {e['stem']: e for e in entries}
        need(len(by_stem) == len(entries) and failed['stem'] in by_stem and
             by_stem[failed['stem']]['operation_id'] == failed['operation_id'], '失败批次不在原state中')
        completed_stems = {v['stem'] for v in completed}
        untouched_stems = {v['stem'] for v in untouched}
        need(len(completed_stems) == len(completed) and len(untouched_stems) == len(untouched) and
             not (completed_stems & untouched_stems or completed_stems & {failed['stem']} or untouched_stems & {failed['stem']}),
             '恢复批次身份重复')
        need({e['stem'] for e in entries if e.get('completed')} == completed_stems and
             {e['stem'] for e in entries if e.get('operation_id') and not e.get('completed')} == {failed['stem']} | untouched_stems,
             '恢复清单未覆盖原state所有有候选批次')
        for row in entries:
            if not row.get('operation_id'):
                need(row['selected_count'] == 0, '未列出的原批次仍有候选')

        generation = self.order.config['generation']
        old_plans = {}
        allrefs = []
        def load_plan(row):
            path, packet = reference(row['prepare'])
            need(path == self.order.path(row['stem']+'-prepare.json'), '准备回执路径与原批次名不符')
            summary = packet.get('summary', {})
            need(summary.get('order_nos') and summary.get('plant'), '原准备回执缺少订单/工厂范围')
            validate_plan(packet, summary['order_nos'], summary['plant'], generation)
            old_plans[row['stem']] = packet
            allrefs.append(path)
            return path, packet

        failed_path, failed_plan = load_plan(failed)
        commit_path, commit = reference(failed['commit'])
        status_path, status = reference(failed['status'])
        result_path, source_result = reference(failed['result'])
        need(commit.get('ok') is False and commit.get('error_type') == 'System.InvalidOperationException'
             and commit.get('error') == '订单版本或状态在准备后变化，未生成。'
             and commit.get('state') != 'completed', '失败回执不是本次明确版本拒绝')
        need(status.get('ok') is True and status.get('operation_id') == failed['operation_id']
             and status.get('state') == 'prepared', '失败操作状态不是prepared')
        source_ids = sorted({v for c in failed_plan['summary']['selected'] for v in c['source_ids']})
        old_existing = set(failed_plan['summary'].get('existing_workorder_ids', []))
        need(source_ids == sorted(failed['source_ids']) and source_result.get('ok') is True
             , '失败操作实际源关联不完整')
        validate_failed_actual_result(source_ids,source_result,old_existing)
        allrefs.extend([commit_path,status_path,result_path])

        discard_records = []; discard_paths=[]; discarded_by_id={}; discard_refs_by_id={}
        for item in rec['discards']:
            request_path, request = reference(item['request'])
            result_path, result = reference(item['result'])
            op = item['operation_id']
            discard_records.append((op,request,result)); discard_paths.extend([request_path,result_path])
            discarded_by_id[op] = result
            discard_refs_by_id[op]=manifest_ref(self.base,result_path)
        pending_rows = [failed, *untouched]
        pending_ids = {x['operation_id'] for x in pending_rows}
        validate_discard_set(discard_records,pending_ids,allow_subset=True)
        allrefs.extend(discard_paths)

        for row in untouched:
            need(row['stem'] in by_stem and by_stem[row['stem']]['operation_id'] == row['operation_id']
                 and not by_stem[row['stem']].get('completed'), '未执行批次身份不符')
            path, packet = load_plan(row)
            need(not (self.order.path(row['stem']+'-started.json').exists() or
                      self.order.path(row['stem']+'-commit.json').exists()), '宣称未执行的批次存在started/commit')
        # Verified completed receipts are the only source of retained generated documents.
        completed_rows = []; completed_summaries = []
        for row in completed:
            need(row['stem'] in by_stem and by_stem[row['stem']].get('completed') is True
                 and by_stem[row['stem']]['operation_id'] == row['operation_id'], '已完成批次身份不符')
            _, plan = load_plan(row)
            receipt_path, receipt = reference(row['commit'])
            docs = validate_generated(receipt, plan); completed_rows.extend(docs)
            completed_summaries.append(plan['summary']); allrefs.append(receipt_path)
        need(len({r['ID'] for r in completed_rows}) == len(completed_rows), '恢复成功回执中工单ID重复')
        need({r['ID'] for r in completed_rows} == {r['ID'] for r in old_order_state['generation'].get('documents', [])},
             '原state.documents与四份实际commit校验结果不符')
        audit_path, audit = reference(rec['audit']); allrefs.append(audit_path)
        audited = audit.get('successful_documents', [])
        need(audit.get('schema_version') == 1 and audit.get('success_count') == len(completed_rows) and len(audited) == len(completed_rows)
             and {(x['id'],x['doc_no']) for x in audited} == {(x['ID'],x['DOC_NO']) for x in completed_rows},
             '恢复审计与已完成工单回执不是同一完整集合')

        # Retain every original zero-candidate batch as an explicit no-op entry.
        old_scope_path = self.order.path('generation/scope.json'); old_scope = read(old_scope_path)
        scope_batches = {x['stem']: x for x in old_scope['batches']}
        zero_entries = [e for e in entries if not e.get('operation_id')]
        zero_paths=[]
        for zero in zero_entries:
            need(zero['selected_count'] == 0, '零操作批次仍报告有候选')
            zero_path = self.order.path(zero['stem']+'-prepare.json')
            zero_plan = read(zero_path); zs = zero_plan['summary']
            validate_plan(zero_plan, zs['order_nos'], zs['plant'], generation)
            need(zs['selected_count'] == 0 and zero_plan.get('no_changes') is True, '原零候选批次回执不再为空')
            zero_paths.append(zero_path)
        allrefs.extend([old_scope_path,*zero_paths])

        baseline_rows = list(failed_plan['summary']['selected'])
        for row in untouched: baseline_rows.extend(old_plans[row['stem']]['summary']['selected'])
        need(baseline_rows, '没有未完成的候选需要恢复准备')
        need(len({r['candidate_id'] for r in baseline_rows}) == len(baseline_rows), '未完成候选存在重复候选ID')
        selected_sources = {source for candidate in baseline_rows for source in candidate['source_doc_nos']}
        order_packet = self.order.read('orders/complete.json')
        need(order_packet.get('complete') is True and order_packet.get('has_next_page') is False,
             '原订单来源清单不完整')
        chosen = []
        for order_row in order_packet['rows']:
            if any(source == order_row['DOC_NO'] or source.startswith(order_row['DOC_NO']+'-') for source in selected_sources):
                chosen.append(order_row['DOC_NO'])
        need(chosen and all(sum(s == n or s.startswith(n+'-') for n in chosen) == 1 for s in selected_sources),
             '未完成候选未唯一对应真实来源订单')

        manual_prepared_path = self.manual.directory/'prepared.json'
        manual_prepared = manual_prepared_path
        mp = read(manual_prepared)
        need(mp.get('ok') is True and isinstance(mp.get('count'),int), 'manual19原准备不完整，不能安全接续')

        # Ask E10 once per unique reconciliation attempt. Results use unique paths and
        # are checkpointed so later continuation never overwrites original evidence.
        old_operation_evidence={}; old_attempt='reconcile-'+uuid.uuid4().hex[:12]
        for row in (pending_rows if not resuming else []):
            plan = old_plans.get(row['stem'])
            if plan is None:
                _, plan = load_plan(row)
            original_existing = set(plan['summary'].get('existing_workorder_ids', []))
            operation_id = row['operation_id']
            status_rel=f'generation/recovery/{old_attempt}/{operation_id}-status.json'
            live_status = await self.order.call('e10_operation_status',status_rel,operation_id=operation_id)
            status_file=self.order.path(status_rel); allrefs.append(status_file)
            if live_status.get('ok') is True:
                need(live_status.get('operation_id') == operation_id and live_status.get('state') == 'prepared',
                     '待处理操作状态不是可安全丢弃的prepared；停止恢复')
            else:
                need(operation_id in discarded_by_id, '操作状态未知且没有持久化discard凭据；停止恢复')
            source_ids = sorted({v for c in plan['summary']['selected'] for v in c['source_ids']})
            observed=[]; actual_refs=[]
            for index in range(0,len(source_ids),100):
                chunk=source_ids[index:index+100]
                actual_rel=f'generation/recovery/{old_attempt}/{operation_id}-actual-{index//100:03}.json'
                actual=await self.order.call('e10_query_order_workorder_result',actual_rel,source_ids=chunk)
                actual_file=self.order.path(actual_rel); allrefs.append(actual_file); actual_refs.append(manifest_ref(self.base,actual_file))
                need(actual.get('ok') is True and actual.get('has_next_page') is False
                     and sorted(actual.get('source_ids',[])) == chunk, '实际关联工单查询不完整；停止恢复')
                observed.extend(actual.get('rows',[]))
            new_ids={str(r.get('ID')) for r in observed if str(r.get('ID')) not in original_existing}
            need(not new_ids, '实际查询发现原待处理批次已有新增工单；停止重准备并人工核对')
            discard_ref=None
            if live_status.get('ok') is True:
                discard_rel=f'generation/recovery/{old_attempt}/{operation_id}-discard.json'
                discarded=await self.order.call('e10_discard_operation',discard_rel,operation_id=operation_id)
                discard_file=self.order.path(discard_rel); allrefs.append(discard_file)
                need(discarded.get('ok') is True and discarded.get('discarded') is True
                     and discarded.get('saved') is False, '旧内存草稿未确认丢弃；停止重准备')
                discarded_by_id[operation_id]=discarded
                discard_ref=manifest_ref(self.base,discard_file)
            elif operation_id in {x['operation_id'] for x in rec['discards']}:
                discard_ref=discard_refs_by_id[operation_id]
            need(discard_ref is not None, '旧operation缺少经过验证的discard证据')
            old_operation_evidence[operation_id]={'status':manifest_ref(self.base,status_file),
                'actual':actual_refs,'discard':discard_ref}

        # Archive the original failure exactly once. The checkpoint is written before
        # each prepare request so a timeout can never be mistaken for a safe retry.
        if not resuming:
            archive.mkdir(parents=True)
            manual_source = Path(read(manual_prepared_path)['source_snapshot']).resolve()
            archive_paths = {self.state_path,branch_plan_path,order_state_path,old_scope_path,
                             manual_prepared_path,manual_source,self.order.path('generation/completion.json'),
                             recovery_path,*allrefs}
            archive_paths.update(self.directory.glob('failure-*.json'))
            order_failure=self.order.path('failure.json')
            if order_failure.is_file(): archive_paths.add(order_failure)
            for entry in entries:
                for suffix in ('-prepare.json','-discovery-prepare.json','-discovery-discard.json','-started.json','-commit.json'):
                    file = self.order.path(entry['stem']+suffix)
                    if file.is_file(): archive_paths.add(file)
            archive_hashes = {}; archive_copies = {}; archive_files={}
            for index, source in enumerate(sorted(archive_paths)):
                if source.is_file():
                    target = archive/'files'/f'{index:04}-{source.name}'
                    target.parent.mkdir(parents=True,exist_ok=True)
                    target.write_bytes(source.read_bytes())
                    sha=digest(source); archive_hashes[str(source)] = sha; archive_copies[source] = target
                    archive_files[str(source)]={'path':str(target),'sha256':digest(target)}
            evidence_paths=set(archive_paths)
            # These are live workflow state/plan files updated when the recovered
            # plan is merged; immutable copies remain protected in archive_files.
            mutable_paths={self.state_path,order_state_path,branch_plan_path,old_scope_path}
            origin_hashes={str(p):digest(p) for p in evidence_paths if p.is_file() and p not in mutable_paths}
            checkpoint={'schema_version':1,'phase':'preparing','recovery_path':str(recovery_path),
                'recovery_sha256':digest(recovery_path),'session_id':self.session_id,
                'origin_sha256':origin_hashes,'archive_files':archive_files,'batches':{},
                'old_operations_reconciled':True,'old_operation_evidence':old_operation_evidence}
            covered=set()
            for row in pending_rows:
                old_plan=old_plans[row['stem']]
                selected_orders=selected_source_orders(old_plan['summary']['order_nos'],old_plan['summary']['selected'])
                need(not covered.intersection(selected_orders),'多个未完成批次的保护订单范围重叠')
                covered.update(selected_orders)
                new_stem='generation/resume-'+uuid.uuid4().hex[:10]
                checkpoint['batches'][row['stem']]={'source_stem':row['stem'],'source_operation_id':row['operation_id'],
                    'source_orders':selected_orders,'stem':new_stem,'state':'pending','attempts':[]}
            save(checkpoint_path,checkpoint)
        else:
            archive_hashes={}; archive_copies={}; archive_files=checkpoint.get('archive_files',{})
            origin_hashes=checkpoint.get('origin_sha256',{})
            need(checkpoint.get('recovery_path')==str(recovery_path)
                 and checkpoint.get('origin_sha256')=={p:digest(Path(p)) for p in origin_hashes},
                 '恢复原始证据已变化；不得续接checkpoint')
            for source, info in archive_files.items():
                target=Path(info['path'])
                need(target.is_file() and digest(target)==info['sha256']
                     and (source not in origin_hashes or digest(Path(source))==origin_hashes[source]),
                     'checkpoint归档/原始证据哈希不一致')
                archive_hashes[source]=info['sha256']; archive_copies[Path(source)]=target
            need(set(checkpoint.get('batches',{}))=={r['stem'] for r in pending_rows},
                 'checkpoint未精确覆盖原待处理批次')

        new_entries=[]; new_plans=[]; reprepare_covered=set()
        for row in pending_rows:
            old_plan=old_plans[row['stem']]
            selected_orders=selected_source_orders(old_plan['summary']['order_nos'],old_plan['summary']['selected'])
            need(not reprepare_covered.intersection(selected_orders),'多个未完成批次的保护订单范围重叠')
            reprepare_covered.update(selected_orders)
            item=checkpoint['batches'][row['stem']]
            need(item['source_orders']==selected_orders and item['source_operation_id']==row['operation_id'],
                 'checkpoint批次来源身份改变')
            new_stem=item.get('prepared_stem',item['stem'])
            prepare_path=self.order.path(item['packet_path']) if item.get('packet_path') else self.order.path(new_stem+'-prepare.json')
            reuse_prepared=recovery_batch_start(checkpoint,row['stem'])
            if reuse_prepared:
                age=time.time()-item.get('returned_epoch',0)
                need(item.get('session_id')==self.session_id and 0 <= age < 29*60,
                     'checkpoint草稿会话已变化或已过期；保留证据，不重建')
                need(prepare_path.is_file() and digest(prepare_path)==item.get('packet_sha256'),
                     'checkpoint成功prepare包缺失或哈希改变')
                status_rel=f'generation/recovery/checkpoint-{uuid.uuid4().hex[:12]}/{item["operation_id"]}-status.json'
                status_path=self.order.path(status_rel)
                try:
                    status=await self.order.call('e10_operation_status',
                        status_rel,operation_id=item['operation_id'])
                except Exception as exc:
                    item.setdefault('status_checks',[]).append({'path':str(status_path),'error':str(exc)[:500],
                        'checked_utc':datetime.now(timezone.utc).isoformat(),'session_id':self.session_id})
                    item['state']='unknown'; item['blocking_error']=str(exc)[:500]; save(checkpoint_path,checkpoint); raise
                item['status_path']=str(status_path); item['status_sha256']=digest(status_path); item['status_packet']=status
                item.setdefault('status_checks',[]).append({'path':str(status_path),'sha256':digest(status_path),
                    'checked_utc':datetime.now(timezone.utc).isoformat(),'session_id':self.session_id})
                if status.get('ok') is not True or status.get('operation_id')!=item['operation_id'] or status.get('state')!='prepared':
                    item['state']='blocked'; save(checkpoint_path,checkpoint)
                    raise ValueError('checkpoint草稿状态未知或不再prepared；保留证据，不重试')
                save(checkpoint_path,checkpoint)
                new_packet=read(prepare_path)
            elif item['state']=='rejected':
                # A returned explicit refusal has no operation to replay. This is a
                # deliberate continuation command; ambiguous transport failures stay blocked.
                item['state']='pending'; save(checkpoint_path,checkpoint)
                new_packet=None
            else:
                need(item['state']=='pending', 'checkpoint不允许准备该批次')
                new_packet=None
            if new_packet is None:
                attempt_stem=item['stem']+'-attempt-'+str(len(item['attempts'])+1).zfill(2)
                new_stem=attempt_stem; prepare_path=self.order.path(new_stem+'-prepare.json')
                item['state']='in_flight'; item['attempts'].append({'stem':new_stem,
                    'started_utc':datetime.now(timezone.utc).isoformat(),'session_id':self.session_id}); save(checkpoint_path,checkpoint)
                try:
                    new_packet=await self.order.call('e10_prepare_packaging_order_batch',new_stem+'-prepare.json',
                        order_nos=selected_orders,**{k:v for k,v in generation.items()
                        if k not in ('date_from','excluded_sales_departments','excluded_order_tokens','included_order_nos')})
                except Exception as exc:
                    item['state']='unknown'; item['blocking_error']=str(exc)[:500]; save(checkpoint_path,checkpoint); raise
                result_state=record_recovery_prepare(checkpoint,row['stem'],new_packet,prepare_path,self.session_id)
                item['prepared_stem']=new_stem
                if result_state=='rejected':
                    save(checkpoint_path,checkpoint)
                    raise ValueError('新恢复准备明确拒绝；保留回执，可显式续接此批')
                if result_state=='unknown':
                    save(checkpoint_path,checkpoint)
                    raise ValueError('新恢复准备回执状态不明确；保留回执，禁止重试')
                save(checkpoint_path,checkpoint)
            summary=validate_plan(new_packet,selected_orders,failed_plan['summary']['plant'],generation)
            expected=old_plan['summary']['selected']
            if candidate_semantics(summary['selected']) != candidate_semantics(expected):
                item['state']='blocked'; item['blocking_error']='新计划候选语义与未完成范围不同'; save(checkpoint_path,checkpoint)
                raise ValueError('新计划候选语义与未完成范围不同；保留证据并停止')
            item['validated']=True; save(checkpoint_path,checkpoint)
            entry={'stem':new_stem,'operation_id':new_packet.get('operation_id'),'selected_count':len(summary['selected']),
                   'source_order_nos':selected_orders,'created':item['returned_epoch'],'session':self.order.session}
            new_entries.append(entry); new_plans.append((new_stem,new_packet,summary))
        need(reprepare_covered == set(chosen) and
             candidate_semantics([c for _,_,s in new_plans for c in s['selected']]) == candidate_semantics(baseline_rows),
             '分批新准备结果未覆盖全部未完成候选；保留证据并停止')
        merged = merge_recovered_generation(old_order_state,new_entries,completed_rows)

        keep_stems = completed_stems | {z['stem'] for z in zero_entries}
        batches = [scope_batches[s['stem']] for s in old_scope['batches'] if s['stem'] in keep_stems]
        batches.extend(new_entries)
        merged_scope = dict(old_scope)
        merged_scope['batches'] = batches
        need(sum(int(item.get('selected_count',0)) for item in batches) == int(old_scope['selected_count']),
             '保留成功批次与新准备批次未覆盖原完整候选集合')
        merged_scope['recovery'] = {'verified_existing':len(completed_rows),'pending_selected':len(baseline_rows)}
        self.order.write('generation/scope.json',merged_scope)
        merged['documents'] = list(old_order_state['generation']['documents'])
        call_count = self.order.state['calls']
        self.order.state = dict(old_order_state); self.order.state['calls']=call_count
        self.order.state.pop('in_flight',None); self.order.state['generation']=merged
        self.order.persist()

        evidence = {str(p):digest(p) for p in [self.order.path('generation/scope.json'),manual_prepared]}
        if mp.get('source_snapshot'):
            evidence[str(Path(mp['source_snapshot']))]=digest(Path(mp['source_snapshot']))
        for new_stem,_,_ in new_plans:
            evidence[str(self.order.path(new_stem+'-prepare.json'))]=digest(self.order.path(new_stem+'-prepare.json'))
        for row in completed:
            for ref in (row['prepare'],row['commit']):
                p,_=reference(ref); evidence[str(p)]=digest(p)
        for zero_path in zero_paths: evidence[str(zero_path)]=digest(zero_path)
        for source, target in archive_copies.items():
            evidence[str(target)]=digest(target)
        evidence[str(recovery_path)]=digest(recovery_path)
        resolved={'recovery':str(recovery_path),'recovery_sha256':digest(recovery_path),
                  'archive':str(archive),'archive_sha256':archive_hashes,'audit':str(audit_path),
                  'new_prepares':[str(self.order.path(stem+'-prepare.json')) for stem,_,_ in new_plans]}
        prepared={'ok':True,'complete':True,'session_id':self.session_id,'evidence_sha256':evidence,
                  'input_hashes':self.input_hashes,'orders':merged_scope,'manual19_count':mp.get('count',0),
                  'manual19_unique_groups':mp.get('unique_groups',0),'resolved_evidence':resolved}
        save(self.directory/'prepared.json',prepared)
        self.state.pop('failure',None); self.state.pop('in_flight',None)
        self.state.setdefault('resolved_recoveries',[]).append(resolved)
        self.state.update(stage='prepared',prepared_sha256=digest(self.directory/'prepared.json'))
        save(self.state_path,self.state)
        checkpoint['phase']='complete'; checkpoint['completed_utc']=datetime.now(timezone.utc).isoformat()
        checkpoint['prepared_sha256']=self.state['prepared_sha256']; save(checkpoint_path,checkpoint)
        return {'ok':True,'phase':'workorders_reconcile_prepare','verified_existing':len(completed_rows),
                'pending_selected':len(baseline_rows),'source_orders':len(chosen),'prepared_file':str(self.directory/'prepared.json'),
                'prepared_sha256':self.state['prepared_sha256'],'saved':False}

    async def prepare(self):
        self.ready(); need(self.state['stage'] == 'new', '已准备或执行，不能覆盖现有分支计划')
        scope = read(self.manual_scope_path)
        source = local(self.base,scope['source_snapshot'])
        complete_manual_scope(scope, read(source))
        await self.order.prepare_generation()
        if scope['specs']:
            result = await self.manual.prepare(self.manual_scope_path)
            self.check_manual_actor()
        else:
            source = local(self.base,scope['source_snapshot'])
            no_manual_needed(scope,read(source))
            self.manual.directory.mkdir(parents=True,exist_ok=False)
            save(self.manual.directory/'prepared.json', {'ok':True,'empty':True,'count':0,'packets':[],
                 'session_id':self.session_id,'scope':str(self.manual_scope_path),'scope_sha256':digest(self.manual_scope_path),
                 'source_snapshot':str(source),'source_sha256':digest(source)})
            result = {'prepared_count':0,'unique_groups':0}
        evidence = {str(p):digest(p) for p in [self.order.path('generation/scope.json'),self.manual.directory/'prepared.json']}
        evidence[str(source)] = digest(source)
        for name in read(self.manual.directory/'prepared.json').get('evidence_sha256', {}):
            p = self.manual.directory/name; evidence[str(p)] = digest(p)
        for entry in self.order.state['generation']['entries']:
            p = self.order.path(entry['stem']+'-prepare.json'); evidence[str(p)] = digest(p)
        prepared = {'ok':True,'complete':True,'session_id':self.session_id,'evidence_sha256':evidence,
                    'input_hashes':self.input_hashes,'orders':self.order.read('generation/scope.json'),
                    'manual19_count':result['prepared_count'],'manual19_unique_groups':result['unique_groups']}
        save(self.directory/'prepared.json',prepared)
        self.state.update(stage='prepared',prepared_sha256=digest(self.directory/'prepared.json'))
        save(self.state_path,self.state)
        return {'ok':True,'phase':'workorders_prepare','order_selected':prepared['orders']['selected_count'],
                'manual19_count':result['prepared_count'],'prepared_file':str(self.directory/'prepared.json'),
                'prepared_sha256':self.state['prepared_sha256'],'saved':False}

    def created_gate(self, allow_approval_failure=False):
        """Validate actual saved receipts, not a subagent's ready/complete summary."""
        self.ready(allow_approval_failure=allow_approval_failure)
        need(self.order.state.get('generation',{}).get('stage') == 'completed', '订单开单尚未完成')
        order_path = self.order.path('generation/completion.json')
        order_rows = documents(read(order_path),'generated_count')
        actual = []
        for entry in self.order.state['generation']['entries']:
            if entry['operation_id']:
                need(entry.get('completed') is True, '订单分批开单尚未完成')
                actual.extend(validate_generated(self.order.read(entry['stem']+'-commit.json'),self.order.read(entry['stem']+'-prepare.json')))
        need(actual == order_rows, '订单完成清单与实际提交回执不一致')
        manual_path = self.manual.directory/'creation-completion.json'
        need(manual_path.exists(), '手工来源开单尚未完成；缺少分支不能视作零新增')
        manual_packet = read(manual_path); manual_rows = documents(manual_packet,'created_count')
        cp = read(self.manual.directory/'checkpoint.json')
        need(cp.get('ok') is True and not cp.get('in_flight') and not cp.get('failure') and cp.get('stage') in ('created','approved'), '手工来源分支状态未确认')
        prepared = read(self.manual.directory/'prepared.json')
        need(manual_packet['prepared_sha256'] == digest(self.manual.directory/'prepared.json') and len(manual_rows) == prepared['count'], '手工来源完成范围与准备不符')
        if prepared.get('empty'):
            need(manual_packet.get('empty') is True and not manual_rows, '零新增分支凭据不一致')
        else:
            for i,(row,packet) in enumerate(zip(manual_rows,prepared['packets'])):
                created = read(self.manual.directory/f'create-{i:03}.json')
                need(created.get('ok') and created.get('state') == 'completed' and created['operation_id'] == packet['operation_id'], '手工工单正式回执不完整')
                same_document(created['result'],read(self.manual.directory/packet['receipt'])['summary']['draft'],'N')
                need((row['ID'],row['DOC_NO']) == (created['result']['MO_ID'],created['result']['DOC_NO']), '手工工单完成清单身份不符')
        all_rows = [dict(ID=r['ID'],DOC_NO=r['DOC_NO'],branch=kind) for kind,rows in [('orders',order_rows),('manual19',manual_rows)] for r in rows]
        need(len({r['ID'] for r in all_rows}) == len(all_rows) and len({r['DOC_NO'] for r in all_rows}) == len(all_rows), '两个分支复用了同一正式工单')
        return {'schema_version':1,'ok':True,'complete':True,'phase':'all_workorders_created','session_id':self.session_id,
                'branches':{'orders':{'receipt':str(order_path),'sha256':digest(order_path),'count':len(order_rows)},
                            'manual19':{'receipt':str(manual_path),'sha256':digest(manual_path),'count':len(manual_rows)}},
                'documents':all_rows,'input_hashes':self.input_hashes}

    async def create(self, approved_sha256, source_check):
        self.ready(); need(self.state['stage'] == 'prepared', '两类完整准备未完成或已有执行，不重放')
        prepared_path = self.directory/'prepared.json'; prepared = read(prepared_path)
        need(digest(prepared_path) == approved_sha256 == self.state['prepared_sha256'] and prepared['session_id'] == self.session_id, '完整计划未核对、已变化或不属当前会话')
        for name,expected in prepared['evidence_sha256'].items():
            need(digest(Path(name)) == expected, '分支准备证据已变化')
        need(source_check.is_file(), '写前来源证据文件不存在')
        scope = read(self.manual_scope_path); snapshot = read(source_check)
        complete_manual_scope(scope,snapshot)
        manual_prepared = self.manual.directory/'prepared.json'
        manual_plan = read(manual_prepared)
        check_basis = validate_source_check(scope, manual_plan['source_snapshot'], source_check,
                                            manual_plan['source_sha256'])
        need(not (self.manual.directory/'checkpoint.json').exists(), '手工分支已有执行检查点，不重放')
        if scope['specs']:
            age = (datetime.now(timezone.utc)-datetime.fromisoformat(manual_plan['first_prepare_utc'])).total_seconds()
            need(manual_plan['session_id'] == self.session_id and 0 <= age < 25*60, '手工来源草稿会话不符或已接近过期')
        need(all(e['session'] == self.session_id and 0 <= time.time()-e['created'] < 25*60
                 for e in self.order.state['generation']['entries'] if e['operation_id'] and not e.get('completed')),
             '订单草稿会话不符或已接近过期')
        status = await self.call('e10_connection_status')
        need(status.get('ok') and status.get('authenticated') is True, '认证失效，未开始正式开单')
        # One shared writer; no branch is approved here, including zero-order branches.
        await self.order.generate()
        if scope['specs']:
            await self.manual.create(digest(manual_prepared),source_check)
        else:
            save(self.manual.directory/'checkpoint.json',{'ok':True,'stage':'created','created':[],'confirmed':[],'bindings':[]})
            save(self.manual.directory/'creation-completion.json', {'ok':True,'complete':True,'empty':True,'created_count':0,'approved_count':0,
                 'documents':[],'bindings':[],'prepared_sha256':digest(manual_prepared),'source_check':str(source_check),'source_check_sha256':digest(source_check),
                 'source_check_basis':check_basis})
        gate = self.created_gate(); save(self.directory/'creation-gate.json',gate)
        self.state.update(stage='created',gate_sha256=digest(self.directory/'creation-gate.json'))
        save(self.state_path,self.state)
        manual_creation = read(self.manual.directory/'creation-completion.json')
        manual_creation['source_check_basis'] = check_basis
        save(self.manual.directory/'creation-completion.json',manual_creation)
        return {'ok':True,'phase':'all_workorders_created','orders_created':gate['branches']['orders']['count'],
                'manual19_created':gate['branches']['manual19']['count'],'approved_count':0,'gate':str(self.directory/'creation-gate.json')}

    def verify_gate(self):
        need(self.state['stage'] in ('created','approval_collected','completed'), '两类开单完成前禁止收集审核或提交审核')
        path = self.directory/'creation-gate.json'; need(path.exists() and digest(path) == self.state.get('gate_sha256'), '两类开单完成凭据缺失或已变化')
        old = read(path)
        allow_approval_failure = (self.state.get('stage') == 'approval_collected'
                                  and self.order.state.get('failure',{}).get('job') == 'approve'
                                  and self.order.state.get('in_flight'))
        current = self.created_gate(allow_approval_failure=bool(allow_approval_failure))
        # Saved business receipts survive a later authenticated session; RAM drafts do not.
        need({k:v for k,v in old.items() if k!='session_id'} == {k:v for k,v in current.items() if k!='session_id'}, '两类开单真实证据已变化')
        return old

    def manual_validator(self, number, document, approval):
        rows = read(self.manual.directory/'creation-completion.json')['documents']
        for index,row in enumerate(rows):
            if row['DOC_NO'] == number:
                same_document(document,read(self.manual.directory/f'create-{index:03}.json')['result'],approval)
                need(document['DOC_NO'] == number, '统一审核的手工工单号码不符')
                return

    async def collect_approval(self):
        self.ready(); self.verify_gate()
        need(self.state['stage'] == 'created', '审核清单已收集或完成，先使用已有回执')
        manual_docs = read(self.manual.directory/'creation-completion.json')['documents']
        result = await self.order.collect_approval(additional_generated=manual_docs,document_validator=self.manual_validator)
        pending_path = self.order.path('approval/pending.json')
        self.state.update(stage='approval_collected',pending_sha256=digest(pending_path))
        save(self.state_path,self.state)
        return {'ok':True,'phase':'workorders_collect_approval',**result,'pending':str(pending_path),'pending_sha256':self.state['pending_sha256']}

    async def approve(self, approved_sha256):
        self.ready(); self.verify_gate()
        need(self.state['stage'] == 'approval_collected', '须在两类开单后完整收集一次统一审核清单')
        need(digest(self.order.path('approval/pending.json')) == approved_sha256 == self.state['pending_sha256'], '统一审核清单未核对或已变化')
        try:
            result = await self.order.approve(document_validator=self.manual_validator)
        except Exception as exc:
            if not self.order.state.get('failure'):
                self.order.state['failure'] = {'job':'approve','error_type':type(exc).__name__,
                                                'error':str(exc)[:1200],'automatic_retry':False}
                self.order.persist()
            raise
        manual_docs = read(self.manual.directory/'creation-completion.json')['documents']
        approved = {}
        batch_path = self.order.path('approval/batch-commit.json')
        if batch_path.exists():
            batch = read(batch_path)
            if batch.get('ok') is True and batch.get('state') == 'completed':
                for item in batch.get('result',{}).get('items',[]):
                    if item.get('state') == 'completed' and isinstance(item.get('result'),dict):
                        approved[item['document_no']] = item['result']
        for row in manual_docs:
            number = row['DOC_NO']
            if number in approved:
                self.manual_validator(number,approved[number],'Y')
                continue
            options = [('approval/documents/'+number+'-commit.json','result'),
                       ('approval/documents/'+number+'-already.json','document'),
                       ('approval/already/'+number+'.json','document')]
            for name,key in options:
                p = self.order.path(name)
                if p.exists():
                    packet = read(p); need(packet.get('ok') is True, '统一审核回执失败')
                    approved[number] = packet[key]; self.manual_validator(number,packet[key],'Y'); break
            need(number in approved, '手工工单缺少实际审核Y回执')
        if manual_docs:
            self.manual.complete_after_approval(approved)
        else:
            save(self.manual.directory/'completion.json',{'ok':True,'complete':True,'empty':True,'created_count':0,'approved_count':0,'bindings':[]})
        completion = {'ok':True,'complete':True,'phase':'all_workorders_approved','creation_gate':str(self.directory/'creation-gate.json'),
                      'orders_created':len(read(self.order.path('generation/completion.json'))['documents']),
                      'manual19_created':len(manual_docs),'approval':result,'manual_completion':str(self.manual.directory/'completion.json'),
                      'remaining_matching_and_picking_gate_required':True}
        save(self.directory/'completion.json',completion)
        self.state['stage'] = 'completed'; save(self.state_path,self.state)
        return {'ok':True,'phase':'all_workorders_approved',**result,'completion':str(self.directory/'completion.json')}

    async def resume_approval(self, recovery_path):
        self.verify_gate()
        need(self.state['stage'] == 'approval_collected' and self.order.state.get('in_flight')
             and self.order.state.get('failure'), '續辦只允許在保留中的審核失敗/未知狀態執行')
        recovery_path = Path(recovery_path).resolve()
        recovery_sha = digest(recovery_path)
        recovery = read(recovery_path)
        original_state_path = self.order.path('state.json')
        preserved_state_path = self.order.path('approval/state-before-continuation-' + uuid.uuid4().hex + '.json')
        preserved_state_path.parent.mkdir(parents=True, exist_ok=True)
        preserved_state_path.write_bytes(original_state_path.read_bytes())
        preserved_state_sha = digest(preserved_state_path)
        result = await self.order.resume_approval(recovery)
        need(digest(recovery_path) == recovery_sha, '续办恢复证据执行期间发生变化')
        result['input_recovery_path'] = str(recovery_path)
        result['input_recovery_sha256'] = recovery_sha
        result['original_state_path'] = str(preserved_state_path)
        result['original_state_sha256'] = preserved_state_sha
        self.state.setdefault('approval_continuations', {})[result['continuation_id']] = {
            'path': result['path'], 'pending_sha256': result['pending_sha256'],
            'recovery_path': result['recovery_path'], 'recovery_sha256': result['recovery_sha256'],
            'input_recovery_path': str(recovery_path), 'input_recovery_sha256': recovery_sha,
            'original_state_path': str(preserved_state_path), 'original_state_sha256': preserved_state_sha,
            'stage':'frozen'}
        save(self.state_path,self.state)
        return result

    async def approve_continuation(self, continuation_id, approved_sha256):
        self.verify_gate()
        saved = self.state.get('approval_continuations', {}).get(continuation_id)
        need(saved and saved['stage'] == 'frozen' and saved['pending_sha256'] == approved_sha256,
             '續辦清單未明確核對或已執行')
        root = self.order.path('approval/continuations/' + continuation_id)
        recovery_path = root/'recovery.json'
        need(digest(recovery_path) == saved['recovery_sha256'], '续办恢复证据已变化')
        recovery_check = read(recovery_path)
        for evidence in recovery_check['excluded_documents'] + [recovery_check['operation_status'], recovery_check['batch_commit']]:
            evidence_path = self.order.path(evidence['path'])
            need(digest(evidence_path) == evidence['sha256'], '续办依赖证据已变化，未开始新批次')
        result = await self.order.approve_continuation(continuation_id, approved_sha256, self.manual_validator)
        need(digest(root/'recovery.json') == saved['recovery_sha256'], '续办恢复证据已变化')
        pending = read(root/'approval'/'pending.json')
        recovery = read(root/'recovery.json')
        manual_docs = read(self.manual.directory/'creation-completion.json')['documents']
        approved = {}
        for receipt_path in (self.order.path('approval/batch-commit.json'), root/'approval'/'batch-commit.json'):
            if receipt_path.exists():
                receipt = read(receipt_path)
                result_items = (receipt.get('result') or {}).get('items', [])
                for item in result_items:
                    if item.get('state') == 'completed' and isinstance(item.get('result'),dict):
                        approved[item['document_no']] = item['result']
        excluded_by_no = {}
        original = read(self.order.path('approval/pending.json'))
        number_by_id = {row['ID']:row['DOC_NO'] for row in original['all_rows']}
        excluded_docs = {}
        for evidence in recovery['excluded_documents']:
            evidence_path=self.order.path(evidence['path'])
            need(digest(evidence_path) == evidence['sha256'], '排除项实际N证据已变化')
            packet = read(evidence_path)
            excluded_docs[packet['document']['MO_ID']] = packet['document']
        for row_id, reason in recovery['excluded_reasons'].items():
            number = number_by_id[row_id]
            if any(doc['DOC_NO'] == number for doc in manual_docs):
                excluded_by_no[number] = {'reason':reason,'actual_document':excluded_docs[row_id]}
        for row in manual_docs:
            number = row['DOC_NO']
            if number in excluded_by_no:
                self.manual_validator(number, excluded_by_no[number]['actual_document'], 'N')
                continue
            if number not in approved:
                for name,key in [('approval/documents/'+number+'-already.json','document'),
                                 ('approval/already/'+number+'.json','document')]:
                    p = self.order.path(name)
                    if p.exists():
                        packet=read(p)
                        if packet.get('ok') is True: approved[number]=packet[key]
                        break
            need(number in approved, '手工工单缺少续办前或续办后的实际Y回执')
            self.manual_validator(number,approved[number],'Y')
        if manual_docs:
            manual_completion = self.manual.complete_after_approval(approved, excluded_by_no, continuation_id)
        else:
            path = self.manual.directory/('completion-'+continuation_id+'.json')
            save(path,{'ok':True,'complete':True,'empty':True,'created_count':0,'approved_count':0,'bindings':[],
                       'excluded_count':0,'excluded_bindings':[],'continuation_id':continuation_id})
            manual_completion={'completion':str(path),'approved':0,'excluded':0}
        excluded_workorders = [{'id':row_id,'document_no':number_by_id[row_id],
                                'reason':recovery['excluded_reasons'][row_id],
                                'actual_status':'N','actual_document':excluded_docs[row_id]}
                               for row_id in recovery['excluded_reasons']]
        completion_path = self.directory/('completion-resumed-'+continuation_id+'.json')
        completion = {'ok':True,'complete':True,'phase':'all_workorders_approved_with_exclusions',
            'creation_gate':str(self.directory/'creation-gate.json'),
            'orders_created':len(read(self.order.path('generation/completion.json'))['documents']),
            'manual19_created':len(manual_docs),'approval':result,'manual_completion':manual_completion['completion'],
            'excluded_workorders':excluded_workorders,'continuation_id':continuation_id,
            'initial_failure_state':str(self.order.path('state.json')),
            'original_state_snapshot':saved['original_state_path'],
            'original_state_snapshot_sha256':saved['original_state_sha256'],
            'original_batch_commit':str(self.order.path('approval/batch-commit.json')),
            'original_batch_commit_sha256':digest(self.order.path('approval/batch-commit.json')),
            'continuation_pending':str(root/'approval'/'pending.json'),
            'continuation_pending_sha256':digest(root/'approval'/'pending.json'),
            'continuation_recovery':str(root/'recovery.json'),
            'continuation_recovery_sha256':digest(root/'recovery.json'),
            'remaining_matching_and_picking_gate_required':True}
        save(completion_path,completion)
        saved['stage']='completed'; saved['completion']=str(completion_path)
        self.state['stage']='completed'; save(self.state_path,self.state)
        return {'ok':True,'phase':'all_workorders_approved_with_exclusions',**result,'completion':str(completion_path),
                'excluded_count':len(excluded_workorders)}

    async def run(self, command):
        phase = command['phase']
        if phase == 'status':
            return {'ok':not bool(self.state.get('failure')),'phase':self.state['stage'],'state':str(self.state_path),'failure':self.state.get('failure')}
        try:
            if phase == 'prepare': return await self.prepare()
            if phase in ('reconcile_prepare','resume_prepare'):
                return await self.resume_prepare(local(self.base,command['recovery']))
            if phase == 'create': return await self.create(command['approved_sha256'],local(self.base,command['source_check']))
            if phase == 'collect_approval': return await self.collect_approval()
            if phase == 'approve': return await self.approve(command['approved_sha256'])
            if phase == 'resume_approval': return await self.resume_approval(local(self.base,command['recovery']))
            if phase == 'approve_continuation': return await self.approve_continuation(command['continuation_id'],command['approved_sha256'])
            raise ValueError('未知双分支阶段')
        except Exception as exc:
            failure = {'ok':False,'phase':phase,'stopped':True,'error':str(exc),'automatic_retry':False}
            if not self.state.get('failure'):
                self.state['failure'] = failure; save(self.state_path,self.state)
            save(self.directory/('failure-'+uuid.uuid4().hex+'.json'),failure)
            return {**failure,'state':str(self.state_path)}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description='Local packing-order recovery evidence helper; performs no E10 calls.')
    parser.add_argument('--build-recovery-manifest',action='store_true')
    parser.add_argument('--base')
    parser.add_argument('--run-dir')
    parser.add_argument('--status')
    parser.add_argument('--result')
    parser.add_argument('--audit')
    parser.add_argument('--out')
    args=parser.parse_args()
    if args.build_recovery_manifest:
        need(all((args.base,args.run_dir,args.status,args.result,args.audit,args.out)),
             '须提供--base --run-dir --status --result --audit --out')
        built=build_recovery_manifest(args.base,args.run_dir,args.status,args.result,args.audit,args.out)
        print(json.dumps({'ok':True,'manifest':str(Path(args.out).resolve()),'failed':built['failed']['stem'],
                          'untouched':len(built['untouched']),'completed':len(built['completed']),'discards':0},ensure_ascii=False))
    else:
        parser.error('须指定--build-recovery-manifest')
