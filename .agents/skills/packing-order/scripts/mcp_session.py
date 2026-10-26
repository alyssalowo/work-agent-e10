#!/usr/bin/env python3
"""Keep one standard MCP session open; never call ERP components directly."""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

from manual_workorders import run_manual
from workorder_branches import WorkorderBranches
from session_output import SessionOutput, compact, print_json
from runtime_preflight import inspect_runtime
from session_commands import (ToolCatalog, prepare_reads, read_request, resolve_command,
                              validate_capabilities, validate_command)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def local(base, name):
    path = (base / name).resolve()
    if not path.is_relative_to(base) or path == base:
        raise ValueError('请求和结果路径必须在本轮工作目录内')
    return path


async def collect(call, command, base, emit=print_json):
    filters = command['filters']
    if set(filters) - {'applicant_name','approval_status','date_from','detail_close','workorder_no','workorder_nos'} or not any(filters.values()):
        raise ValueError('须指定已核对的受支持查询条件')
    if 'workorder_nos' in filters:
        numbers = filters['workorder_nos']
        if (not isinstance(numbers, list) or not 1 <= len(numbers) <= 500
                or any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9]+-[A-Za-z0-9]+', v) for v in numbers)
                or len(numbers) != len(set(numbers))
                ):
            raise ValueError('workorder_nos须为1至500个唯一完整工单号')
        if filters.get('workorder_no'):
            raise ValueError('workorder_nos与workorder_no不能同时使用')
    details = command.get('include_detail', True)
    target = local(base, command['output'])
    directory = local(base, command['pages'])
    if target.exists() or (directory.exists() and any(directory.iterdir())):
        raise ValueError('查询输出已存在；使用新的本轮目录，不混用旧分页')
    rows, seen, start, pages = [], set(), 1, 0
    while True:
        response = await call('e10_query_picking_workflow', **filters, include_detail=details,
                              start_row=start, max_rows=1000)
        pages += 1
        save(directory / f'{pages:04}.json', response)
        if not response.get('ok'):
            raise RuntimeError('查询失败，部分分页已保留；没有生成完整源')
        if response.get('start_row') != start or response.get('include_detail') != details:
            raise ValueError('分页身份不符')
        if response.get('filters') != filters:
            raise ValueError('筛选条件在分页间改变')
        for row in response['rows']:
            key = (row.get('ID'), row.get('DETAIL_ID')) if details else row.get('ID')
            if not row.get('ID') or (details and not row.get('DETAIL_ID')) or key in seen:
                raise ValueError('分页出现重复或缺少ID，须核实，未继续')
            seen.add(key)
            rows.append(row)
        if pages % 5 == 0:
            emit({'phase':'query','pages':pages,'rows':len(rows)})
        if response.get('has_next_page') is False:
            break
        expected = start + len(response['rows'])
        if not response['rows'] or response.get('next_start_row') != expected:
            raise ValueError('分页没有可靠进展，未继续')
        start = expected
    packet = {'ok':True,'complete':True,'has_next_page':False,'include_detail':details,
              'filters':filters,'page_count':pages,'rows':rows}
    save(target, packet)
    return {'ok':True,'phase':'query','rows':len(rows),'pages':pages,'output':str(target)}


async def approve(call, command, base, emit=print_json):
    pending = read(local(base, command['pending']))
    name, employee_id = command['applicant_name'], command['applicant_id']
    rows = pending['rows']
    if pending.get('ok') is not True or pending.get('complete') is not True or pending.get('has_next_page') is not False or pending.get('include_detail') is not False:
        raise ValueError('待审核单头清单尚未确认完整')
    if not name or not employee_id or any(r.get('APPLICANT_NAME') != name or r.get('APPLICANT_ID') != employee_id or r.get('APPROVAL_STATUS') != 'N' for r in rows):
        raise ValueError('申请人或未审核范围不符')
    numbers = [row['DOC_NO'] for row in rows]
    if len(set(numbers)) != len(numbers):
        raise ValueError('待审核清单含重复单据')
    fingerprint = hashlib.sha256(json.dumps([name,employee_id,numbers], ensure_ascii=False).encode()).hexdigest()
    checkpoint = local(base, command['checkpoint'])
    receipts = local(base, command['receipts'])
    state = read(checkpoint) if checkpoint.exists() else {'fingerprint':fingerprint,'confirmed':[],'already_confirmed':[],'ok':False}
    if state.get('fingerprint') != fingerprint or state.get('in_flight') or state.get('failure'):
        raise ValueError('检查点不符或有未解决的执行状态；先查询操作记录及实际单据，不重放')
    done = set(state['confirmed']) | set(state['already_confirmed'])
    save(checkpoint, state)
    for number in numbers:
        if number in done:
            continue
        # All operations below still pass through ClientSession.call_tool.
        prepared = await call('e10_prepare_document', module='ISSUE_RECEIPT_REQ', action='confirm', document_no=number)
        save(receipts / (number + '-prepare.json'), prepared)
        if not prepared.get('ok'):
            state['failure'] = {'document_no':number,'stage':'prepare','response':prepared}
            save(checkpoint, state)
            return state
        if prepared.get('no_changes'):
            state['already_confirmed'].append(number)
            done.add(number)
            save(checkpoint, state)
            continue
        draft = prepared['summary']['draft']
        if draft['Owner_Emp'] != employee_id or draft['ApproveStatus'] != 'N':
            await call('e10_discard_operation', operation_id=prepared['operation_id'])
            state['failure'] = {'document_no':number,'stage':'scope_check','reason':'单据申请人或状态已变化'}
            save(checkpoint, state)
            return state
        state['in_flight'] = {'document_no':number,'operation_id':prepared['operation_id']}
        save(checkpoint, state)
        committed = await call('e10_commit_operation', operation_id=prepared['operation_id'])
        save(receipts / (number + '-commit.json'), committed)
        if not committed.get('ok') or committed.get('state') != 'completed' or committed.get('result', {}).get('ApproveStatus') != 'Y':
            state['failure'] = {'document_no':number,'stage':'commit','response':committed}
            save(checkpoint, state)
            return state
        state.pop('in_flight')
        state['confirmed'].append(number)
        done.add(number)
        save(checkpoint, state)
        if len(state['confirmed']) % 10 == 0:
            emit({'phase':'approve','confirmed':len(state['confirmed']),'total':len(numbers)})
    state['ok'] = True
    save(checkpoint, state)
    return {'ok':True,'phase':'approve','confirmed':len(state['confirmed']),
            'already_confirmed':len(state['already_confirmed']),'checkpoint':str(checkpoint)}


async def run_workorders(call, command, base, session_id, runs, emit=print):
    directory = local(base, command['directory'])
    config_path = local(base, command['config'])
    key = str(directory)
    if key not in runs:
        runs[key] = WorkorderBranches(call, base, directory, config_path, session_id, emit)
    work = runs[key]
    if work.config_path != config_path:
        raise ValueError('同一分支批次不能切换配置文件')
    return await work.run(command)


async def dispatch_command(call, command, base, session_id, branch_runs, emit=print_json):
    """One command at a time; presentation never controls a business decision."""
    command, proof = resolve_command(command, base)
    validate_command(command, base)
    catalog = getattr(call, 'tool_catalog', None)
    if catalog:
        validate_capabilities(command, catalog)
    answer = await _dispatch_validated(call, command, base, session_id, branch_runs, emit)
    if proof:
        answer = {**answer, 'command_source': proof}
    return answer


async def _dispatch_validated(call, command, base, session_id, branch_runs, emit):
    if command.get('job') == 'read_sequence':
        return await read_sequence(call, command, base)
    if command.get('job') == 'collect':
        return await collect(call, command, base, emit)
    if command.get('job') == 'approve':
        return await approve(call, command, base, emit)
    if command.get('job') == 'workorders':
        return await run_workorders(call, command, base, session_id, branch_runs, emit)
    if command.get('job') in ('manual_prepare','manual_create','manual_execute','manual_confirm'):
        return await run_manual(call, command, base, session_id, emit)
    request, target = read_request(base, command, getattr(call, 'validate_request', None))
    if request['name'] == 'e10_commit_operation':
        operation_id = request['arguments']['operation_id']
        if len(operation_id) != 32 or any(c not in '0123456789abcdef' for c in operation_id):
            raise ValueError('operation_id格式无效')
        marker = local(base, 'commit-started-' + operation_id + '.json')
        # Atomic guard: never call a commit twice, including racing local jobs.
        with marker.open('x', encoding='utf-8') as output:
            json.dump({'operation_id':operation_id,'output':str(target)}, output, ensure_ascii=False)
    answer = await call(request['name'], **request.get('arguments', {}))
    save(target, answer)
    result = {k:answer[k] for k in ('ok','state','operation_id','no_changes','error') if k in answer}
    result['output'] = str(target)
    return result


async def read_sequence(call, command, base):
    """Batch known independent reads; authentication and all writes stay explicit."""
    prepared = prepare_reads(command, base, getattr(call, 'validate_request', None))
    completed = []
    for request, target in prepared:
        answer = await call(request['name'], **request.get('arguments', {}))
        save(target, answer)
        completed.append({'tool':request['name'], 'output':str(target), 'ok':answer.get('ok') is True,
                          'summary':compact(answer)})
        if answer.get('ok') is not True:
            return {'ok':False, 'phase':'read_sequence', 'completed':completed,
                    'failed_output':str(target), 'note':'已停止后续请求；认证异常由主代理显式恢复。'}
    return {'ok':True, 'phase':'read_sequence', 'count':len(completed), 'completed':completed}


async def session_loop(args):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    base, server = Path(args.work_dir).resolve(), Path(args.server).resolve()
    if base.is_relative_to(Path(__file__).resolve().parents[1]) or not server.is_file():
        raise ValueError('须指定实际MCP服务器和技能目录之外的本轮工作目录')
    base.mkdir(parents=True, exist_ok=True)
    session_id = uuid.uuid4().hex
    output = SessionOutput(base, session_id)
    branch_runs = {}
    call_sequence = 0
    previous_command_end = None
    params = StdioServerParameters(command=sys.executable, args=[str(server)], cwd=str(server.parent), env={**os.environ,'PYTHONUTF8':'1'})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=330)) as session:
            await session.initialize()
            catalog = await ToolCatalog.load(session)
            save(output.directory/'capabilities.json', catalog.schemas)
            async def call(name, **arguments):
                nonlocal call_sequence
                catalog.validate(name, arguments)
                call_sequence += 1
                metric = {'session_id':session_id,'sequence':call_sequence,'tool':name,
                          'started_utc':datetime.now(timezone.utc).isoformat()}
                tick = time.perf_counter()
                try:
                    result = await session.call_tool(name, arguments)
                    if result.isError:
                        raise RuntimeError('MCP工具调用失败：' + name)
                    answer = result.structuredContent or json.loads(result.content[0].text)
                    if not isinstance(answer, dict):
                        raise ValueError('MCP返回须为对象；未重试')
                    metric.update({k:answer[k] for k in ('ok','state','operation_id') if k in answer})
                    return answer
                except Exception as exc:
                    metric.update(ok=False,exception_type=type(exc).__name__)
                    raise
                finally:
                    metric.update(elapsed_seconds=round(time.perf_counter()-tick,6),
                                  returned_utc=datetime.now(timezone.utc).isoformat())
                    # Arguments, business payloads and credentials never enter the timing log.
                    with (base/'mcp-calls.jsonl').open('a',encoding='utf-8') as output:
                        output.write(json.dumps(metric,ensure_ascii=False)+'\n')
            call.validate_request = catalog.validate
            call.tool_catalog = catalog
            await output.ready(session, getattr(args, 'discover_tools', False), catalog.schemas)
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                if not line:
                    break
                try:
                    command, command_proof = resolve_command(json.loads(line), base)
                    validate_command(command, base)
                    if command.get('exit') is True:
                        break
                    command_start = time.perf_counter()
                    command_metric = {'session_id':session_id, 'ok':False, 'phase':command.get('phase') or command.get('job') or 'request',
                                      'started_utc':datetime.now(timezone.utc).isoformat(),
                                      'since_previous_command_seconds':None if previous_command_end is None else round(command_start-previous_command_end,6)}
                    if command_proof:
                        command_metric['command_source'] = command_proof
                    try:
                        answer = await dispatch_command(call, command, base, session_id, branch_runs, output.progress)
                        if command_proof:
                            answer = {**answer, 'command_source': command_proof}
                        command_metric['ok'] = answer.get('ok') is True
                        output.complete(answer)
                    finally:
                        previous_command_end = time.perf_counter()
                        command_metric.update(elapsed_seconds=round(previous_command_end-command_start,6),
                                              returned_utc=datetime.now(timezone.utc).isoformat())
                        with (base/'workflow-timing.jsonl').open('a',encoding='utf-8') as metric_output:
                            metric_output.write(json.dumps(command_metric,ensure_ascii=False)+'\n')
                except Exception as exc:
                    failure = {'ok':False,'error':str(exc),'note':'未自动重试；已开始的写入先核对检查点和E10实际状态'}
                    try:
                        output.complete(failure)
                    except Exception:
                        # Persistence failure must still surface; never rerun the command.
                        print_json({**failure, 'error':str(exc)[:600], 'result_evidence_error':True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', required=True)
    parser.add_argument('--work-dir', required=True)
    parser.add_argument('--discover-tools', action='store_true', help='Opt in to tool discovery; save names to a local manifest')
    parser.add_argument('--preflight', action='store_true', help='Local runtime check only; do not start MCP or log in')
    args = parser.parse_args()
    base = Path(args.work_dir).resolve()
    if base.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('预检及业务产物必须保存在技能目录之外')
    result = inspect_runtime(args.server)
    save(base/'runtime-preflight.json', result)
    summary = {'ok':result['ok'], 'phase':'local_preflight', 'offline':True,
               'checks':len(result['checks']), 'failed':result['failed'],
               'output':str(base/'runtime-preflight.json')}
    if args.preflight or not result['ok']:
        print_json(summary)
    if not result['ok']:
        raise SystemExit(1)
    if not args.preflight:
        asyncio.run(session_loop(args))


if __name__ == '__main__':
    main()
