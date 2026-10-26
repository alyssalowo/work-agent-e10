#!/usr/bin/env python3
"""One standard MCP session for scoped order generation and workorder approval.

No ERP component access, automatic login, retries, exports, or browser writes.
Each mutating phase is an explicit command after its complete read-only plan.
"""
import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid


class WorkflowError(ValueError):
    pass


BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_doc_date(now=None):
    """Return the current business date in Beijing time, including UTC-boundary cases."""
    instant = datetime.now(BEIJING_TZ) if now is None else now
    need(isinstance(instant, datetime) and instant.tzinfo is not None, '审核日期时钟必须包含时区')
    return instant.astimezone(BEIJING_TZ).date().isoformat()


def valid_doc_date(value):
    if not isinstance(value, str) or re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) is None:
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def need(condition, message):
    if not condition:
        raise WorkflowError(message)


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def number(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9]+-[A-Za-z0-9]+', value) is not None


def quantity(value):
    result = Decimal(str(value))
    need(result.is_finite(), '数量不是有限值')
    return result


def request_config(value):
    need(isinstance(value, dict) and not set(value) - {'generation', 'approval', 'batch_size'}, '请求字段不受支持')
    need(value.get('generation') or 'approval' in value, '须指定订单生成或工单审核阶段')
    batch = value.get('batch_size', 50)
    need(type(batch) is int and 1 <= batch <= 50, 'batch_size 须为1至50，默认50')
    result = {'batch_size': batch}
    if value.get('generation'):
        generation = value['generation']
        required = {'plant_name', 'date_from', 'excluded_sales_departments', 'excluded_order_tokens',
                    'doc_type_code', 'excluded_1_v_name_keywords', 'excluded_1_h_name_keywords'}
        need(isinstance(generation, dict) and not set(generation) - (required | {'included_order_nos'}) and required <= set(generation), '订单生成须提供完整筛选与排除规则')
        need(isinstance(generation['plant_name'], str) and generation['plant_name'].strip(), '须指定完整工厂名称')
        need(re.fullmatch(r'\d{4}-\d{2}-\d{2}', generation['date_from']) is not None, '起始日期须为yyyy-MM-dd')
        date.fromisoformat(generation['date_from'])
        need(generation['doc_type_code'] == '5101', '此流程仅处理5101包装工单')
        for key in required - {'plant_name', 'date_from', 'doc_type_code'}:
            values = generation[key]
            need(isinstance(values, list) and all(isinstance(v, str) and v.strip() for v in values), '排除规则须为字符串列表')
            need(len(values) == len(set(values)), '排除规则不可重复')
        need(all(re.fullmatch(r'[A-Za-z0-9]+-', v) for v in generation['excluded_order_tokens']), '订单排除值须带末尾连字符')
        if 'included_order_nos' in generation:
            included = generation['included_order_nos']
            need(isinstance(included, list) and all(number(v) for v in included), 'included_order_nos须为有效单号列表')
            need(len(included) == len(set(included)), 'included_order_nos不可重复')
        result['generation'] = generation
    if 'approval' in value:
        approval = value['approval']
        need(isinstance(approval, dict) and not set(approval) - {'scope', 'planner_name', 'doc_type_code'}, '审核配置字段不受支持')
        approval = {'scope': 'planner_pending', 'planner_name': '朱浩龙', 'doc_type_code': '5101', **approval}
        need(approval['scope'] in ('planner_pending', 'generated_only'), '审核范围须为planner_pending或generated_only')
        need(isinstance(approval['planner_name'], str) and approval['planner_name'].strip(), '须指定完整生管人员姓名')
        need(approval['doc_type_code'] == '5101', '此流程仅审核5101包装工单')
        need(approval['scope'] != 'generated_only' or 'generation' in result, '仅本批审核须包含本轮生成阶段')
        result['approval'] = approval
    return result


def exclusion(code, name, generation):
    if not code.startswith('1-'):
        return 'not_1_finished_product'
    for prefix, key in (('1-V', 'excluded_1_v_name_keywords'), ('1-H', 'excluded_1_h_name_keywords')):
        if code.upper().startswith(prefix) and any(word.upper() in name.upper() for word in generation[key]):
            return 'excluded_product_name'
    return None


def validate_plan(packet, numbers, plant, generation):
    need(packet.get('ok') is True and packet.get('saved') is False, '准备失败或不是未保存候选')
    summary = packet['summary']
    need(summary['order_nos'] == numbers and summary['plant'] == plant, '准备结果的订单或工厂不符')
    doc = summary['document_type']
    need(doc['CODE'] == generation['doc_type_code'] and doc['NAME'] == '包装工单', '单据类型不符')
    need(summary['doc_default_type'] == '2' and summary['auto_approve'] is False, '指定单别或不自动审核设置不符')
    rules = summary['selection_rules']
    need(rules == {'allowed_item_prefix': '1-', 'excluded_1_v_name_keywords': generation['excluded_1_v_name_keywords'],
                   'excluded_1_h_name_keywords': generation['excluded_1_h_name_keywords']}, '成品排除规则改变')
    selected, excluded = summary['selected'], summary['excluded']
    need(summary['selected_count'] == len(selected) and summary['excluded_count'] == len(excluded)
         and summary['raw_candidate_count'] == len(selected) + len(excluded), '候选数量不一致')
    for row in selected:
        need(exclusion(row['item_code'], row['item_name'], generation) is None and quantity(row['plan_quantity']) > 0, '选中候选违反品号、品名或数量规则')
        need(row['source_doc_nos'] and all(any(source == n or source.startswith(n + '-') for n in numbers)
                                          for source in row['source_doc_nos']), '候选来源不在本批订单内')
        draft = row['draft']
        need(draft['SELECT'] is True and draft['AUTO_APPROVE'] is False and draft['DOC_ID'] == doc['ID'], '候选选择或类型不符')
        need(draft['Owner_Org']['ROid'] == plant['ID'] and draft['Owner_Org']['RTK'] == 'PLANT', '候选工厂不符')
        sources = draft['MO_B002_SALES']
        need(sources and all(source['SELECT'] is True and source['SequenceNumber'] > 0 for source in sources), '来源选择或序号不正确')
        need(set(row['source_ids']) == {source['SALES_ORDER_DOC_SD_ID'] for source in sources}, '来源主键不一致')
        need(row['source_doc_nos'] == [source['DOC_NO'] for source in sources], '来源单号不一致')
        need(quantity(draft['PLAN_QTY']) == quantity(row['plan_quantity']), '候选数量与草稿不一致')
    for row in excluded:
        need(exclusion(row['item_code'], row['item_name'], generation) is not None or quantity(row['plan_quantity']) <= 0, '存在不应排除的候选')
    need(bool(selected) == bool(packet.get('operation_id')), '操作编号与候选不一致')
    need(bool(selected) or packet.get('no_changes') is True, '空候选必须为no_changes')
    return summary


def validate_generated(packet, prepared):
    need(packet.get('ok') is True and packet.get('state') == 'completed'
         and packet['operation_id'] == prepared['operation_id'], '生成未确认完成；停止，不重试')
    summary, result = prepared['summary'], packet['result']
    need(result.get('generated') is True and result['generated_count'] == len(result['documents']), '生成回执不完整')
    expected = Counter((r['item_code'], quantity(r['plan_quantity']), tuple(sorted(r['source_ids']))) for r in summary['selected'])
    actual = defaultdict(list)
    for row in result['documents']:
        need(row['PLANT_ID'] == summary['plant']['ID'] and row['DOC_ID'] == summary['document_type']['ID'], '新增工单工厂或类型不符')
        need(row['DOC_DATE'][:10] == summary['doc_date'] and row['APPROVAL_STATUS'] == 'N', '新增工单日期或审核状态不符')
        need(row['ID'] not in summary.get('existing_workorder_ids', []), '回执包含生成前既有工单')
        actual[row['ID']].append(row)
    observed = Counter()
    numbers = set()
    for rows in actual.values():
        row = rows[0]
        need(row['DOC_NO'] not in numbers and row['DOC_NO'].startswith('5101-'), '生成单号重复或类型不符')
        numbers.add(row['DOC_NO'])
        need(all((r['DOC_NO'], r['ITEM_CODE'], quantity(r['PLAN_QTY'])) ==
                 (row['DOC_NO'], row['ITEM_CODE'], quantity(row['PLAN_QTY'])) for r in rows), '同一工单的关联结果不一致')
        observed[(row['ITEM_CODE'], quantity(row['PLAN_QTY']), tuple(sorted({r['SOURCE_ID'] for r in rows})))] += 1
    need(observed == expected, '实际新增工单与完整候选不一致；停止后续写入并核实')
    return [rows[0] for rows in actual.values()]


def selected_source_orders(batch_numbers, selected):
    """Return only requested order headers that actually source selected candidates."""
    result = set()
    for candidate in selected:
        sources = candidate.get('source_doc_nos')
        need(isinstance(sources, list) and sources, '选中候选缺少订单来源')
        for source in sources:
            matches = [number for number in batch_numbers
                       if source == number or source.startswith(number + '-')]
            need(len(matches) == 1, '选中候选来源无法唯一对应本批订单：' + str(source))
            result.add(matches[0])
    return [number for number in batch_numbers if number in result]


def selected_semantics(rows):
    """Candidate identity independent of transient wizard IDs and generated operation IDs."""
    return Counter((r['item_code'], r['item_name'], quantity(r['plan_quantity']),
                    r['draft']['ITEM_FEATURE_ID'], tuple(sorted(r['source_ids'])),
                    tuple(sorted(r['source_doc_nos']))) for r in rows)


def has_selected_guard_contract(summary, selected_numbers):
    """Accept the optimized server plan only when its commit guard is explicit and complete."""
    if summary.get('commit_guard_scope') != 'selected_source_orders':
        return False
    guarded = summary.get('guarded_order_nos')
    versions = summary.get('order_versions')
    if not isinstance(guarded, list) or guarded != selected_numbers or not isinstance(versions, list):
        return False
    by_number = {}
    ids = set()
    for row in versions:
        if not isinstance(row, dict):
            return False
        doc_no, row_id = row.get('DOC_NO'), row.get('ID')
        if not isinstance(doc_no, str) or not doc_no or not row_id or doc_no in by_number or row_id in ids:
            return False
        version = row.get('VERSION')
        if version is None or isinstance(version, bool) or (isinstance(version, str) and not version.strip()):
            return False
        if 'VERSION' not in row or row.get('APPROVAL_STATUS') != 'Y' or str(row.get('CLOSE')) != '0':
            return False
        by_number[doc_no] = row
        ids.add(row_id)
    return set(by_number) == set(selected_numbers) and len(by_number) == len(selected_numbers)


class Workflow:
    def __init__(self, config, work_dir, transport, emit=None):
        self.config = request_config(config)
        self.base = Path(work_dir).resolve()
        self.evidence_base = self.base
        need(not self.base.is_relative_to(Path(__file__).resolve().parents[1]), '本轮数据不能放在技能目录内')
        self.base.mkdir(parents=True, exist_ok=True)
        self.transport, self.emit = transport, emit or (lambda value: print(json.dumps(value, ensure_ascii=True), flush=True))
        self.session = uuid.uuid4().hex
        self.state = self.read('state.json') if self.path('state.json').exists() else {'config_hash': fingerprint(self.config), 'calls': 0}
        need(self.state['config_hash'] == fingerprint(self.config), '本轮请求改变；不能复用旧计划或回执')
        self.write('request.json', self.config)
        self.persist()

    def path(self, name):
        path = (self.base / name).resolve()
        need(path.is_relative_to(self.base) and path != self.base, '文件必须在本轮工作目录内')
        return path

    def evidence_path(self, name):
        path = (self.evidence_base / name).resolve()
        need(path.is_relative_to(self.evidence_base) and path != self.evidence_base,
             '恢复证据路径必须位于原订单工作目录内')
        return path

    def read(self, name):
        return load(self.path(name))

    def write(self, name, value, exclusive=False):
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
        if exclusive:
            with path.open('x', encoding='utf-8') as stream:
                stream.write(data)
        else:
            temp = path.with_name(path.name + '.tmp')
            temp.write_text(data, encoding='utf-8')
            temp.replace(path)

    def persist(self):
        self.write('state.json', self.state)

    def ready(self):
        need(not self.state.get('in_flight') and not self.state.get('failure'), '有未解决的中断或失败；先查回执和实际单据，不重放')

    async def call(self, name, output, **arguments):
        self.state['calls'] += 1
        sequence, started = self.state['calls'], time.monotonic()
        try:
            result = await self.transport(name, **arguments)
            self.write(output, result)
            return result
        finally:
            self.persist()
            with self.path('calls.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'sequence': sequence, 'tool': name, 'elapsed_seconds': round(time.monotonic() - started, 3), 'output': output}) + '\n')

    async def collect_orders(self):
        generation = self.config['generation']
        filters = {key: generation[key] for key in ('plant_name', 'date_from', 'excluded_sales_departments', 'excluded_order_tokens')}
        rows, seen, numbers, plant, start, page = [], set(), set(), None, 1, 0
        while True:
            page += 1
            result = await self.call('e10_query_order_workorder_sources', f'orders/pages/{page:04}.json', **filters, start_row=start, max_rows=1000)
            need(result.get('ok') is True and result['start_row'] == start, '订单分页查询失败')
            need(all(result['filters'].get(key) == value for key, value in filters.items() if key != 'plant_name'), '订单筛选条件改变')
            if plant is None:
                plant = result['plant']
            need(result['plant'] == plant and plant['NAME'] == filters['plant_name'], '订单查询工厂改变')
            for row in result['rows']:
                need(row['ID'] not in seen and row['DOC_NO'] not in numbers and number(row['DOC_NO']), '订单主键或单号重复/无效')
                need(row['DOC_DATE'][:10] >= filters['date_from'] and row['SALES_DEPARTMENT'] not in filters['excluded_sales_departments'], '订单日期或部门不符')
                need(not any(token.upper() in row['DOC_NO'].upper() for token in filters['excluded_order_tokens']), '订单包含应排除的单别')
                seen.add(row['ID']); numbers.add(row['DOC_NO']); rows.append(row)
            need(result['next_start_row'] == start + len(result['rows']), '订单分页无可靠进展')
            if result['has_next_page'] is False:
                break
            need(result['has_next_page'] is True and result['rows'], '订单分页完整性未知')
            start = result['next_start_row']
        included = generation.get('included_order_nos')
        if included is not None:
            present = {row['DOC_NO'] for row in rows}
            missing = [value for value in included if value not in present]
            packet = {'ok': True, 'complete': True, 'has_next_page': False, 'filters': filters, 'plant': plant,
                      'all_rows': rows, 'rows': [row for row in rows if row['DOC_NO'] in set(included)],
                      'included_order_nos': included}
            self.write('orders/complete.json', packet)
            need(not missing, 'blocked: included_order_nos不存在于完整合规订单查询结果或被筛选条件排除: ' + ','.join(missing))
            return packet
        packet = {'ok': True, 'complete': True, 'has_next_page': False, 'filters': filters, 'plant': plant, 'rows': rows}
        self.write('orders/complete.json', packet)
        return packet

    async def prepare_generation(self):
        self.ready()
        need('generation' in self.config, '未指定生成阶段')
        if self.state.get('generation'):
            scope = self.read('generation/scope.json')
            return {'orders': scope['orders'], 'selected_count': scope['selected_count'], 'no_changes': True, 'plan': 'generation/scope.json'}
        orders = await self.collect_orders()
        entries, selected, excluded, candidate_ids, covered = [], [], [], set(), set()
        generation = self.config['generation']
        for offset in range(0, len(orders['rows']), self.config['batch_size']):
            numbers = [row['DOC_NO'] for row in orders['rows'][offset:offset + self.config['batch_size']]]
            stem = f'generation/{offset:05}'
            discovery = await self.call('e10_prepare_packaging_order_batch', stem + '-discovery-prepare.json', order_nos=numbers,
                                    **{k: generation[k] for k in generation if k not in ('date_from', 'excluded_sales_departments', 'excluded_order_tokens', 'included_order_nos')})
            discovery_summary = validate_plan(discovery, numbers, orders['plant'], generation)
            selected_numbers = selected_source_orders(numbers, discovery_summary['selected'])
            if selected_numbers:
                if has_selected_guard_contract(discovery_summary, selected_numbers):
                    # The updated server keeps full discovery provenance while narrowing
                    # commit-time versions to these selected source orders.
                    packet = discovery
                    summary = discovery_summary
                    self.write(stem + '-prepare.json', packet)
                else:
                    # Legacy service: discard the broad guard and reprepare the same
                    # selected candidates against only their actual source order headers.
                    operation_id = discovery.get('operation_id')
                    need(operation_id, '有选中候选的发现草稿缺少操作编号')
                    discarded = await self.call('e10_discard_operation', stem + '-discovery-discard.json', operation_id=operation_id)
                    need(discarded.get('ok') is True and discarded.get('discarded') is True
                         and discarded.get('saved') is False, '发现草稿未确认安全丢弃；停止，不再准备')
                    packet = await self.call('e10_prepare_packaging_order_batch', stem + '-prepare.json', order_nos=selected_numbers,
                                             **{k: generation[k] for k in generation if k not in ('date_from', 'excluded_sales_departments', 'excluded_order_tokens', 'included_order_nos')})
                    summary = validate_plan(packet, selected_numbers, orders['plant'], generation)
                    need(selected_semantics(summary['selected']) == selected_semantics(discovery_summary['selected']),
                         '缩小订单保护范围后选中候选变化；保留回执并停止')
            else:
                # A no-op has no server operation; retain the complete discovery packet as
                # the canonical batch receipt without making a redundant second request.
                packet = discovery
                summary = discovery_summary
                self.write(stem + '-prepare.json', packet)
            need(not covered.intersection(numbers), '准备批次订单重叠')
            covered.update(numbers)
            for row in summary['selected']:
                need(row['candidate_id'] not in candidate_ids, '候选主键跨批次重复')
                candidate_ids.add(row['candidate_id'])
            selected.extend(summary['selected']); excluded.extend(summary['excluded'])
            entries.append({'stem': stem, 'operation_id': packet.get('operation_id'), 'selected_count': len(summary['selected']),
                            'source_order_nos': selected_numbers,
                            'discovery_stem': stem + '-discovery',
                            'created': time.time(), 'session': self.session})
            self.emit({'phase': 'prepare_generation', 'orders_done': len(covered), 'orders_total': len(orders['rows']), 'selected': len(selected)})
        need(len(covered) == len(orders['rows']), '准备未覆盖全部订单')
        scope = {'ok': True, 'complete': True, 'orders': len(orders['rows']), 'batches': entries, 'selected_count': len(selected),
                 'raw_candidate_count': len(selected) + len(excluded), 'excluded_count': len(excluded),
                 'excluded_reasons': dict(Counter(row['reason'] for row in excluded)),
                 'selected_quantity': str(sum((quantity(row['plan_quantity']) for row in selected), Decimal(0)))}
        self.write('generation/scope.json', scope)
        self.state['generation'] = {'stage': 'prepared', 'entries': entries}
        self.persist()
        return {key: scope[key] for key in ('orders', 'selected_count', 'raw_candidate_count', 'excluded_count', 'excluded_reasons', 'selected_quantity')}

    async def commit(self, operation_id, stem):
        self.ready()
        marker = {'operation_id': operation_id, 'started_utc': datetime.now(timezone.utc).isoformat()}
        self.write(stem + '-started.json', marker, exclusive=True)
        self.state['in_flight'] = {**marker, 'stem': stem}
        self.persist()
        result = await self.call('e10_commit_operation', stem + '-commit.json', operation_id=operation_id)
        need(result.get('ok') is True and result.get('state') == 'completed' and result.get('operation_id') == operation_id, '写入未确认完成；保留记录并停止，不重试')
        return result

    def commit_checked(self):
        self.state.pop('in_flight')
        self.persist()

    async def generate(self):
        self.ready()
        generation = self.state.get('generation', {})
        if generation.get('stage') == 'completed':
            return {'generated_count': len(generation.get('documents', [])), 'no_changes': True, 'receipt': 'generation/completion.json'}
        need(generation.get('stage') == 'prepared', '必须先完成本轮全部候选核对')
        pending = [entry for entry in generation['entries'] if entry['operation_id'] and not entry.get('completed')]
        need(all(entry['session'] == self.session and time.time() - entry['created'] < 29 * 60 for entry in pending), '未执行草稿不在当前会话或接近过期；停止，不自动重建')
        documents = list(generation.get('documents', []))
        for entry in pending:
            prepared = self.read(entry['stem'] + '-prepare.json')
            packet = await self.commit(entry['operation_id'], entry['stem'])
            added = validate_generated(packet, prepared)
            need(not {row['ID'] for row in documents}.intersection(row['ID'] for row in added), '生成结果跨批次重复')
            documents.extend(added)
            entry['completed'] = True
            generation['documents'] = documents
            self.commit_checked()
            self.emit({'phase': 'generate', 'completed_batches': sum(bool(e.get('completed')) for e in generation['entries']), 'generated': len(documents)})
        generation['stage'] = 'completed'
        self.write('generation/completion.json', {'ok': True, 'complete': True, 'generated_count': len(documents), 'documents': documents})
        self.persist()
        return {'generated_count': len(documents), 'receipt': 'generation/completion.json'}

    async def collect_approval(self, additional_generated=None, document_validator=None):
        self.ready()
        need('approval' in self.config, '未指定审核阶段')
        if self.state.get('approval'):
            pending = self.read('approval/pending.json')
            return {'pending_count': len(pending['rows']), 'no_changes': True, 'plan': 'approval/pending.json'}
        approval = self.config['approval']
        filters = {'planner_name': approval['planner_name'], 'doc_type_code': approval['doc_type_code'],
                   'approval_status': 'N', 'doc_date': beijing_doc_date()}
        rows, ids, numbers, start, page, planner, doc = [], set(), set(), 1, 0, None, None
        while True:
            page += 1
            result = await self.call('e10_query_workorder_workflow', f'approval/pages/{page:04}.json', **filters, start_row=start, max_rows=500)
            need(result.get('ok') is True and result['filters'] == filters and result['start_row'] == start, '待审核查询条件或分页不符')
            if planner is None:
                planner, doc = result['planner'], result['document_type']
            need(planner == result['planner'] and doc == result['document_type'] and planner['NAME'] == approval['planner_name']
                 and doc['CODE'] == approval['doc_type_code'] and doc['NAME'] == '包装工单', '生管人员或单据类型不符')
            for row in result['rows']:
                need(row['ID'] not in ids and row['DOC_NO'] not in numbers, '待审核工单重复')
                need(row['PLANNER_ID'] == planner['ID'] and row['PLANNER_NAME'] == approval['planner_name']
                     and row['DOC_ID'] == doc['ID'] and row['DOC_CODE'] == approval['doc_type_code'] and row['APPROVAL_STATUS'] == 'N', '待审核工单范围不符')
                need(str(row.get('DOC_DATE', ''))[:10] == filters['doc_date'], '待审核工单日期不属于本轮北京时间当天')
                ids.add(row['ID']); numbers.add(row['DOC_NO']); rows.append(row)
            need(result['next_start_row'] == start + len(result['rows']), '待审核分页无可靠进展')
            if result['has_next_page'] is False:
                break
            need(result['has_next_page'] is True and result['rows'], '待审核分页完整性未知')
            start = result['next_start_row']
        all_rows, skipped = rows, []
        generated = []
        if approval['scope'] == 'generated_only' or additional_generated is not None:
            need(self.state.get('generation', {}).get('stage') == 'completed', '仅本批审核须先完成本轮生成')
            generated = list(self.read('generation/completion.json')['documents'])
            if additional_generated is not None:
                need(isinstance(additional_generated, list), '另一开单分支回执必须为完整列表')
                generated.extend(additional_generated)
            need(len({r['ID'] for r in generated}) == len(generated) and len({r['DOC_NO'] for r in generated}) == len(generated), '两类新增工单ID或单号重复')
        if approval['scope'] == 'generated_only':
            wanted = {row['ID'] for row in generated}
            rows = [row for row in rows if row['ID'] in wanted]
        by_id = {row['ID']:row for row in all_rows}
        for row in generated:
            if row['ID'] in ids:
                need(by_id[row['ID']]['DOC_NO'] == row['DOC_NO'], '新增回执与待审核单号不一致')
                continue
            current = await self.call('e10_get_document', f'approval/already/{row["DOC_NO"]}.json', module='MO', document_no=row['DOC_NO'])
            draft = current.get('document', {})
            need(current.get('ok') is True and draft.get('MO_ID') == row['ID'] and draft.get('DOC_NO') == row['DOC_NO'] and draft.get('Owner_Emp') == planner['ID']
                 and draft.get('DOC_ID') == doc['ID'] and draft.get('ApproveStatus') == 'Y', '本批工单未在待审核集合内且状态/人员不能确认')
            if document_validator:
                document_validator(row['DOC_NO'], draft, 'Y')
            skipped.append(row['DOC_NO'])
        selected_rows = rows
        packet = {'ok': True, 'complete': True, 'has_next_page': False, 'scope': approval['scope'], 'filters': filters,
                  'planner': planner, 'document_type': doc, 'rows': selected_rows, 'all_rows': all_rows,
                  'already_approved': skipped, 'all_pending_count': len(all_rows),
                  'selected_ids': [row['ID'] for row in selected_rows], 'excluded_ids': [], 'continuation': False}
        self.write('approval/pending.json', packet)
        self.state['approval'] = {'stage': 'collected', 'confirmed': [], 'skipped': skipped}
        self.persist()
        return {'pending_count': len(rows), 'already_approved': len(skipped), 'all_pending_count': len(all_rows), 'scope': approval['scope']}

    async def approve(self, document_validator=None):
        self.ready()
        approval = self.state.get('approval', {})
        if approval.get('stage') == 'completed':
            return {'confirmed_count': len(approval['confirmed']), 'already_approved': len(approval['skipped']), 'no_changes': True}
        need(approval.get('stage') == 'collected', '必须先完整收集待审核清单')
        pending = self.read('approval/pending.json')
        rows = pending['rows']
        need(valid_doc_date(pending.get('filters', {}).get('doc_date')),
             '待审核清单缺少有效冻结日期；请重新collect_approval')
        need(pending.get('complete') is True and pending.get('all_rows') is not None,
             '旧审核清单没有完整冻结查询；请重新collect_approval，不推测缺失行')
        need(not approval['confirmed'] and not approval.get('batch_operation_id'), '已有审核结果；禁止整批重放')
        all_rows = pending['all_rows']
        expected = [{'id': r['ID'], 'document_no': r['DOC_NO'], 'version': r['VERSION']} for r in all_rows]
        selected_ids = [r['ID'] for r in rows]
        excluded_ids = pending.get('excluded_ids', [])
        frozen_ids = {r['ID'] for r in all_rows}
        if pending['scope'] == 'planner_pending':
            need(set(selected_ids) == frozen_ids - set(excluded_ids), 'planner_pending选择须覆盖当前完整N集合扣除明确排除项')
        elif pending['scope'] == 'generated_only':
            need(set(selected_ids) <= frozen_ids, 'generated_only选择超出完整N集合')
        elif pending['scope'] == 'continuation':
            need(pending.get('continuation') is True and set(selected_ids) == set(pending.get('unattempted_ids', [])),
                 '续办选择须严格沿用原未尝试集合')
        else:
            raise WorkflowError('审核清单范围不受支持')
        need(len({x['id'] for x in expected}) == len(expected) and len(selected_ids) == len(set(selected_ids)), '冻结清单包含重复ID')
        if not rows:
            approval['stage'] = 'completed'
            self.write('approval/completion.json', {'ok': True, 'complete': True, 'confirmed': approval['confirmed'],
                'already_approved': approval['skipped'], 'excluded_ids': excluded_ids, 'no_changes': True})
            self.persist()
            return {'confirmed_count': len(approval['confirmed']), 'already_approved': len(approval['skipped']),
                    'excluded_ids': excluded_ids, 'no_changes': True, 'receipt': 'approval/completion.json'}
        batch = await self.call('e10_prepare_workorder_confirmation_batch', 'approval/batch-prepare.json',
            expected_items=expected, excluded_ids=excluded_ids, planner_name=pending['planner']['NAME'],
            doc_type_code=pending['document_type']['CODE'], doc_date=pending['filters']['doc_date'], selected_ids=selected_ids,
            selection_scope=pending['scope'])
        need(batch.get('ok') is True and batch.get('state') == 'prepared' and batch.get('saved') is False
             and batch.get('ready_to_commit') is True
             and batch.get('collected_count') == len(expected)
             and batch.get('selected_count') == len(selected_ids)
             and set(batch.get('excluded_ids', [])) == set(excluded_ids)
             and set(batch.get('selected_ids', [])) == set(selected_ids)
             and batch.get('filters') == pending['filters'], '批量审核准备未完整匹配冻结清单；停止')
        items = batch.get('summary', {}).get('items')
        need(isinstance(items, list) and len(items) == len(rows), '批量准备缺少逐单草稿')
        by_id = {r['ID']: r for r in rows}
        prepared_by_id = {}
        for item in items:
            row = by_id.get(item.get('id'))
            need(row is not None and row['DOC_NO'] == item.get('document_no')
                 and str(row['VERSION']) == str(item.get('version')), '批量准备单据身份或版本不符')
            need(row['ID'] not in prepared_by_id, '批量准备包含重复工单')
            draft = item.get('draft', {})
            need(draft.get('MO_ID') == row['ID'] and draft.get('DOC_NO') == row['DOC_NO']
                 and draft.get('Owner_Emp') == pending['planner']['ID']
                 and draft.get('DOC_ID') == pending['document_type']['ID']
                 and draft.get('ApproveStatus') == 'N' and str(draft.get('Version')) == str(row['VERSION'])
                 and str(draft.get('DOC_DATE', ''))[:10] == pending['filters']['doc_date'],
                 '批量准备草稿身份、人员、类型、状态或版本改变')
            if document_validator:
                document_validator(row['DOC_NO'], draft, 'N')
            prepared_by_id[row['ID']] = row
        need(set(prepared_by_id) == set(selected_ids) and batch.get('operation_id'), '批量准备遗漏或增加单据')
        approval['batch_operation_id'] = batch['operation_id']
        self.state['in_flight'] = {'operation_id': batch['operation_id'], 'stem': 'approval/batch',
                                   'started_utc': datetime.now(timezone.utc).isoformat(), 'kind': 'batch_approval'}
        self.persist()
        committed = await self.call('e10_commit_operation', 'approval/batch-commit.json', operation_id=batch['operation_id'])
        need(committed.get('ok') is True and committed.get('state') == 'completed'
             and committed.get('operation_id') == batch['operation_id'], '批量审核提交结果未知或部分失败；保留回执并停止')
        commit_result = committed.get('result')
        need(isinstance(commit_result, dict), '批量提交结果结构不符；保留回执并停止')
        result_items = commit_result.get('items')
        need(isinstance(result_items, list), '批量提交缺少逐项实际回执；停止后续阶段')
        actual_by_id = {}
        for actual in result_items:
            doc_id = actual.get('id')
            need(doc_id in prepared_by_id and doc_id not in actual_by_id, '批量提交返回未知或重复单据')
            row = prepared_by_id[doc_id]
            document = actual.get('result')
            need(isinstance(document, dict) and actual.get('state') == 'completed' and document.get('MO_ID') == row['ID']
                 and actual.get('document_no') == row['DOC_NO'] and document.get('DOC_NO') == row['DOC_NO']
                 and document.get('Owner_Emp') == pending['planner']['ID']
                 and document.get('DOC_ID') == pending['document_type']['ID'] and document.get('ApproveStatus') == 'Y'
                 and str(document.get('DOC_DATE', ''))[:10] == pending['filters']['doc_date'],
                 '逐项实际审核回执未确认；停止')
            if document_validator:
                document_validator(row['DOC_NO'], document, 'Y')
            actual_by_id[doc_id] = document
            approval['confirmed'].append(row['DOC_NO'])
            self.persist()
        need(set(actual_by_id) == set(selected_ids), '批量提交逐项回执不完整；停止')
        declared_confirmed = commit_result.get('confirmed_ids')
        if declared_confirmed is not None:
            need(isinstance(declared_confirmed, list) and set(declared_confirmed) == set(selected_ids),
                 '批量提交confirmed_ids与逐项回执不符')
        declared_remaining = commit_result.get('remaining_ids')
        if declared_remaining is not None:
            need(declared_remaining == [], '批量成功回执仍包含未尝试工单')
        need(commit_result.get('failed_item') is None, '批量成功回执同时报告失败工单')
        self.commit_checked()
        approval['stage'] = 'completed'
        self.write('approval/completion.json', {'ok': True, 'complete': True, 'confirmed': approval['confirmed'], 'already_approved': approval['skipped']})
        self.persist()
        return {'confirmed_count': len(approval['confirmed']), 'already_approved': len(approval['skipped']), 'receipt': 'approval/completion.json'}

    async def resume_approval(self, recovery):
        """Freeze a new approval set only from proven unattempted items after explicit exclusions."""
        original = self.read('approval/pending.json')
        need(original.get('complete') is True and isinstance(original.get('all_rows'), list), '原审核查询清单不完整，无法续办')
        need(valid_doc_date(original.get('filters', {}).get('doc_date')),
             '原审核清单缺少冻结日期，不能跨日期续办；请按新一轮收集')
        need(isinstance(recovery, dict) and set(recovery) == {
            'original_pending_sha256', 'excluded_ids', 'excluded_documents', 'unattempted_ids',
            'excluded_reasons', 'operation_status', 'batch_commit'}, '续办证据字段不完整')
        need(recovery['original_pending_sha256'] == hashlib.sha256(
            self.path('approval/pending.json').read_bytes()).hexdigest(), '续办证据引用的原清单哈希不符')
        def evidence_packet(entry, label):
            need(isinstance(entry, dict) and set(entry) == {'path', 'sha256'}, label + '证据引用格式不符')
            path = self.path(entry['path'])
            need(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == entry['sha256'], label + '证据文件缺失或哈希改变')
            return json.loads(path.read_text(encoding='utf-8-sig'))
        receipt = evidence_packet(recovery['batch_commit'], '原批量提交')
        need(Path(recovery['batch_commit']['path']).as_posix().endswith('approval/batch-commit.json'), '批量提交证据不是原始回执')
        operation_status = evidence_packet(recovery['operation_status'], '原操作状态')
        current_operation = self.state.get('in_flight', {}).get('operation_id')
        need(self.state.get('failure',{}).get('job') == 'approve' and current_operation
             and receipt.get('operation_id') == current_operation and receipt.get('state') in ('unknown','partial','rejected')
             and operation_status.get('ok') is True and operation_status.get('operation_id') == current_operation
             and operation_status.get('state') in ('unknown', 'partial', 'failed', 'rejected'), '原操作状态证据未指向失败/未知的批量操作')
        result = receipt.get('result') or {}
        remaining = result.get('remaining_ids')
        need(isinstance(remaining, list) and len(remaining) == len(set(remaining)), '原提交回执未证明准确未尝试集合')
        excluded_ids, unattempted_ids = recovery['excluded_ids'], recovery['unattempted_ids']
        reasons = recovery['excluded_reasons']
        need(isinstance(excluded_ids, list) and isinstance(unattempted_ids, list)
             and len(excluded_ids) == len(set(excluded_ids)) and len(unattempted_ids) == len(set(unattempted_ids))
             and set(unattempted_ids) == set(remaining) - set(excluded_ids)
             and not set(excluded_ids) & set(unattempted_ids),
             '续办集合必须精确等于原回执的未尝试项，且与排除项互斥')
        original_by_id = {row['ID']: row for row in original['rows']}
        need(set(excluded_ids) | set(unattempted_ids) <= set(original_by_id), '续办集合含原清单外单据')
        commit_items = result.get('items', [])
        need(isinstance(commit_items, list), '原提交逐项回执格式不符')
        completed_ids, failed_ids = set(), set()
        for item in commit_items:
            row_id = item.get('id')
            need(row_id in original_by_id and row_id not in completed_ids and row_id not in failed_ids,
                 '原提交回执包含未知或重复工单')
            if item.get('state') == 'completed':
                actual = item.get('result')
                need(isinstance(actual, dict) and actual.get('MO_ID') == row_id
                     and actual.get('DOC_NO') == original_by_id[row_id]['DOC_NO']
                     and actual.get('ApproveStatus') == 'Y', '原批量成功项缺少实际Y回执')
                completed_ids.add(row_id)
            elif item.get('state') in ('failed', 'unknown', 'rejected'):
                failed_ids.add(row_id)
            else:
                raise WorkflowError('原提交项状态未知；不能生成续办清单')
        declared_completed = result.get('confirmed_ids')
        if declared_completed is not None:
            need(isinstance(declared_completed, list) and set(declared_completed) == completed_ids,
                 '原回执confirmed_ids与逐项实际Y结果不符')
        failed_item = result.get('failed_item')
        if failed_item is not None:
            need(isinstance(failed_item, dict) and failed_item.get('id') in original_by_id,
                 '原失败项不在冻结清单内')
            failed_ids.add(failed_item['id'])
        need(set(excluded_ids) <= (set(remaining) | failed_ids) and failed_ids <= set(excluded_ids)
             and not completed_ids & (set(excluded_ids) | set(unattempted_ids)),
             '失败/未知项须明确排除，已确认Y项不得进入续办')
        need(isinstance(reasons, dict) and set(reasons) == set(excluded_ids)
             and all(isinstance(v, str) and v.strip() for v in reasons.values()), '每个排除项须保留用户给出的原因')
        docs = recovery['excluded_documents']
        need(isinstance(docs, list) and len(docs) == len(excluded_ids), '缺少排除项实际单据证据')
        observed_excluded = set()
        for evidence in docs:
            packet = evidence_packet(evidence, '排除项实际单据')
            doc = packet.get('document', {})
            need(packet.get('ok') is True, '排除项实际单据查询失败')
            row = original_by_id[doc['MO_ID']]
            need(doc['MO_ID'] in excluded_ids and doc['MO_ID'] not in observed_excluded, '实际单据证据超出或重复明确排除集合')
            need(doc.get('DOC_NO') == row['DOC_NO'] and doc.get('Owner_Emp') == original['planner']['ID']
                 and doc.get('DOC_ID') == original['document_type']['ID'] and doc.get('ApproveStatus') == 'N'
                 and str(doc.get('DOC_DATE', ''))[:10] == original['filters']['doc_date'],
                 '排除项实际单据身份或N状态不符')
            observed_excluded.add(doc['MO_ID'])
        need(observed_excluded == set(excluded_ids), '排除项实际单据证据不完整')

        filters = original['filters']
        rows, ids, numbers, start, page = [], set(), set(), 1, 0
        while True:
            page += 1
            result = await self.call('e10_query_workorder_workflow', f'approval/continuation-query/{page:04}.json',
                                     **filters, start_row=start, max_rows=500)
            need(result.get('ok') is True and result.get('filters') == filters and result.get('start_row') == start,
                 '续办当前N查询条件或分页不符')
            need(result.get('planner') == original['planner'] and result.get('document_type') == original['document_type'],
                 '续办生管或单据类型已变化')
            for row in result['rows']:
                need(row.get('ID') not in ids and row.get('DOC_NO') not in numbers
                     and row.get('PLANNER_ID') == original['planner']['ID']
                     and row.get('DOC_ID') == original['document_type']['ID']
                     and row.get('APPROVAL_STATUS') == 'N'
                     and str(row.get('DOC_DATE', ''))[:10] == filters['doc_date'],
                     '续办当前N清单出现重复、越界或不同日期单据')
                ids.add(row['ID']); numbers.add(row['DOC_NO']); rows.append(row)
            need(result.get('next_start_row') == start + len(result['rows']), '续办N分页没有可靠进展')
            if result.get('has_next_page') is False:
                break
            need(result.get('has_next_page') is True and result['rows'], '续办N清单分页完整性未知')
            start = result['next_start_row']
        need((set(excluded_ids) | set(unattempted_ids)) <= ids, '排除项或原未尝试项不在当前完整N查询内')
        by_id = {row['ID']: row for row in rows}
        selected = [by_id[row['ID']] for row in original['rows'] if row['ID'] in set(unattempted_ids)]
        packet = {'ok': True, 'complete': True, 'has_next_page': False, 'scope': 'continuation',
                  'filters': filters, 'planner': original['planner'], 'document_type': original['document_type'],
                  'all_rows': rows, 'rows': selected, 'all_pending_count': len(rows),
                  'selected_ids': [row['ID'] for row in selected], 'excluded_ids': excluded_ids,
                  'excluded_reasons': reasons,
                  'continuation': True, 'source_scope': original['scope'],
                  'original_pending_sha256': recovery['original_pending_sha256'],
                  'unattempted_ids': unattempted_ids}
        continuation_id = uuid.uuid4().hex
        directory = self.path('approval/continuations/' + continuation_id)
        directory.mkdir(parents=True, exist_ok=False)
        recovery_path = directory / 'recovery.json'
        recovery_path.write_text(json.dumps(recovery, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        target = directory / 'approval'
        target.mkdir()
        pending_path = target / 'pending.json'
        pending_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        digest = hashlib.sha256(pending_path.read_bytes()).hexdigest()
        manifest = {'continuation_id': continuation_id, 'original_pending_sha256': recovery['original_pending_sha256'],
                    'excluded_ids': excluded_ids, 'excluded_reasons': reasons,
                    'unattempted_ids': unattempted_ids, 'pending_sha256': digest,
                    'path': str(pending_path), 'recovery_path': str(recovery_path),
                    'recovery_sha256': hashlib.sha256(recovery_path.read_bytes()).hexdigest()}
        (directory / 'continuation.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return {'ok': True, 'phase': 'resume_approval', **manifest}

    async def approve_continuation(self, continuation_id, approved_sha256, document_validator=None):
        need(re.fullmatch(r'[a-f0-9]{32}', continuation_id) is not None, '续办ID无效')
        root = self.path('approval/continuations/' + continuation_id)
        pending_path = root / 'approval' / 'pending.json'
        need(pending_path.is_file(), '续办冻结清单缺失')
        actual_sha = hashlib.sha256(pending_path.read_bytes()).hexdigest()
        need(actual_sha == approved_sha256, '续办清单未核对或已变化')
        manifest_path = root / 'continuation.json'
        need(manifest_path.is_file(), '续办证据索引缺失')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
        recovery_path = root / 'recovery.json'
        need(manifest.get('continuation_id') == continuation_id and manifest.get('pending_sha256') == actual_sha
             and recovery_path.is_file() and hashlib.sha256(recovery_path.read_bytes()).hexdigest() == manifest.get('recovery_sha256'),
             '续办清单或恢复证据索引已变化')
        recovery = json.loads(recovery_path.read_text(encoding='utf-8-sig'))
        for evidence in recovery['excluded_documents'] + [recovery['operation_status'], recovery['batch_commit']]:
            evidence_path = self.evidence_path(evidence['path'])
            need(evidence_path.is_file() and hashlib.sha256(evidence_path.read_bytes()).hexdigest() == evidence['sha256'],
                 '续办依赖证据已变化；未开始新批次')
        clone = Workflow(self.config, root, self.transport, self.emit)
        clone.evidence_base = self.evidence_base
        clone.logged_in = True
        clone.write('approval/pending.json', json.loads(pending_path.read_text(encoding='utf-8')))
        clone.state['approval'] = {'stage': 'collected', 'confirmed': [], 'skipped': []}
        clone.persist()
        result = await clone.approve(document_validator=document_validator)
        return {'continuation_id': continuation_id, 'continuation_sha256': actual_sha, **result}

    async def run(self, job):
        try:
            if job == 'login':
                if getattr(self, 'logged_in', False):
                    return {'ok': True, 'authenticated': True, 'no_changes': True}
                result = await self.call('e10_login', 'login.json')
                need(result.get('ok') is True and result.get('authenticated') is True, '登录未成功；不自动重试')
                self.logged_in = True
                return {'ok': True, 'authenticated': True}
            if job == 'logout':
                if not getattr(self, 'logged_in', False):
                    return {'ok': True, 'no_changes': True}
                result = await self.call('e10_logout', 'logout.json')
                self.logged_in = False
                return {'ok': result.get('ok') is True}
            need(getattr(self, 'logged_in', False), '请先显式登录当前会话')
            actions = {'prepare_generation': self.prepare_generation, 'generate': self.generate,
                       'collect_approval': self.collect_approval, 'approve': self.approve}
            need(job in actions, '未知阶段命令')
            result = await actions[job]()
            return {'ok': True, 'phase': job, **result}
        except Exception as exc:
            failure = {'job': job, 'error_type': type(exc).__name__, 'error': str(exc)[:1200], 'automatic_retry': False}
            if self.state.get('failure'):
                failure = self.state['failure']
            else:
                self.write('failure.json', failure)
                self.state['failure'] = failure
                self.persist()
            return {'ok': False, **failure, 'details': 'failure.json', 'state': 'state.json'}


async def serve(args):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    server = Path(args.server).resolve()
    need(server.is_file(), '须指定已安装的E10 MCP服务器路径')
    params = StdioServerParameters(command=sys.executable, args=[str(server)], cwd=str(server.parent), env={**os.environ, 'PYTHONUTF8': '1'})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=330)) as session:
            await session.initialize()
            async def call(name, **arguments):
                result = await session.call_tool(name, arguments)
                need(not result.isError, 'MCP工具执行错误：' + name)
                return result.structuredContent or json.loads(''.join(block.text for block in result.content if block.type == 'text'))
            workflow = Workflow(load(args.request), args.work_dir, call)
            workflow.emit({'ready': True, 'batch_size': workflow.config['batch_size'], 'work_dir': str(workflow.base)})
            try:
                while line := await asyncio.to_thread(sys.stdin.readline):
                    command = json.loads(line)
                    need(isinstance(command, dict) and set(command) == {'job'}, '命令只需job；长清单使用本地计划文件')
                    if command['job'] == 'exit':
                        break
                    workflow.emit(await workflow.run(command['job']))
            finally:
                if getattr(workflow, 'logged_in', False):
                    try:
                        await workflow.run('logout')
                    except Exception:
                        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True)
    parser.add_argument('--server')
    parser.add_argument('--work-dir')
    parser.add_argument('--validate-request', action='store_true', help='Only validate the local request; no MCP process or login')
    args = parser.parse_args()
    if args.validate_request:
        config = request_config(load(args.request))
        print(json.dumps({'ok': True, 'stages': [key for key in ('generation', 'approval') if key in config], 'batch_size': config['batch_size']}))
    else:
        need(args.server and args.work_dir, '运行须指定server及本轮work-dir')
        asyncio.run(serve(args))


if __name__ == '__main__':
    main()
