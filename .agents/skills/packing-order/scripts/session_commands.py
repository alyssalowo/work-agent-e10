"""Validate file-backed session commands before any MCP tool or write marker."""
import argparse
import copy
from datetime import date
import json
from pathlib import Path
import re

from local_evidence import read_bytes, read_json, sha256_bytes, json_bytes
from jsonschema import Draft202012Validator


READ_TOOLS = frozenset('''e10_current_user e10_connection_status e10_lookup_master
e10_lookup_item_feature e10_resolve_picking_applicant e10_query_workorder
e10_get_document e10_query_order_versions e10_operation_status
e10_query_packaging_workorders e10_query_order_workorder_sources
e10_query_order_workorder_result e10_query_workorder_item_date
e10_query_workorder_workflow e10_query_picking_requests e10_query_picking_workflow'''.split())
TEXT = {'type': 'string', 'minLength': 1}
SHA = {'type': 'string', 'pattern': '^[a-fA-F0-9]{64}$'}


def object_schema(properties, required):
    return {'type': 'object', 'properties': properties, 'required': required,
            'additionalProperties': False}


STEP = object_schema({'request': TEXT, 'output': TEXT}, ['request', 'output'])
FILTERS = object_schema({k: TEXT for k in
    ('applicant_name', 'approval_status', 'date_from', 'detail_close', 'workorder_no')}, [])
FILTERS['properties']['workorder_nos'] = {'type':'array','items':TEXT,'minItems':1,'maxItems':500}
FILTERS['minProperties'] = 1
FILTERS['properties']['approval_status'] = {'enum': ['N', 'Y', 'V']}
FILTERS['properties']['detail_close'] = {'enum': ['0', '1', '2']}


def command_schema(command):
    if not isinstance(command, dict):
        raise ValueError('命令须为JSON对象；不要发送对象的显示文本或字符串数组')
    job = command.get('job')
    if 'exit' in command:
        return object_schema({'exit': {'const': True}}, ['exit'])
    if job is None:
        return STEP
    if job == 'read_sequence':
        return object_schema({'job': {'const': job}, 'steps': {
            'type': 'array', 'minItems': 1, 'maxItems': 50, 'items': STEP}}, ['job', 'steps'])
    if job == 'collect':
        return object_schema({'job': {'const': job}, 'filters': FILTERS,
            'include_detail': {'type': 'boolean'}, 'pages': TEXT, 'output': TEXT},
            ['job', 'filters', 'pages', 'output'])
    if job == 'approve':
        return object_schema({'job': {'const': job}, 'pending': TEXT, 'applicant_name': TEXT,
            'applicant_id': TEXT, 'checkpoint': TEXT, 'receipts': TEXT},
            ['job', 'pending', 'applicant_name', 'applicant_id', 'checkpoint', 'receipts'])
    if job == 'workorders':
        phase = command.get('phase')
        required = ['job', 'phase', 'config', 'directory']
        props = {'job': {'const': job}, 'phase': {'enum': ['prepare', 'create',
            'collect_approval', 'approve', 'status', 'reconcile_prepare', 'resume_prepare',
            'resume_approval', 'approve_continuation']},
            'config': TEXT, 'directory': TEXT}
        if phase in ('create', 'approve', 'approve_continuation'):
            props['approved_sha256'] = SHA
            required.append('approved_sha256')
        if phase == 'create':
            props['source_check'] = TEXT
            required.append('source_check')
        if phase in ('reconcile_prepare', 'resume_prepare', 'resume_approval'):
            props['recovery'] = TEXT
            required.append('recovery')
        if phase == 'approve_continuation':
            props['continuation_id'] = {'type':'string','pattern':'^[a-f0-9]{32}$'}
            required.append('continuation_id')
        return object_schema(props, required)
    if job == 'manual_prepare':
        return object_schema({'job': {'const': job}, 'scope': TEXT, 'directory': TEXT},
                             ['job', 'scope', 'directory'])
    if job == 'manual_create':
        return object_schema({'job': {'const': job}, 'directory': TEXT,
            'approved_sha256': SHA, 'source_check': TEXT},
            ['job', 'directory', 'approved_sha256', 'source_check'])
    raise ValueError('未知或禁用的会话job：' + str(job))


def validate(value, schema, label):
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(e.path))
    if errors:
        error = errors[0]
        position = '.'.join(str(x) for x in error.path)
        # Do not echo parameter values (including possible credentials).
        raise ValueError(f'{label}格式不符：{position or "根对象"}（{error.validator}）')


def local_path(base, name):
    base = Path(base).resolve()
    if not isinstance(name, str) or not name.strip():
        raise ValueError('须指定本轮目录内的文件路径')
    path = (base / name).resolve()
    if not path.is_relative_to(base) or path == base:
        raise ValueError('命令、请求和结果路径必须在本轮工作目录内')
    return path


def read_object(path):
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValueError('JSON文件须包含对象：' + Path(path).name)
    return value


def resolve_command(value, base):
    if not isinstance(value, dict):
        raise ValueError('命令须为JSON对象')
    if 'command_file' not in value:
        return copy.deepcopy(value), None
    validate(value, object_schema({'command_file': TEXT, 'command_sha256': SHA}, ['command_file']), '文件命令入口')
    path = local_path(base, value['command_file'])
    raw = read_bytes(path)
    fingerprint = sha256_bytes(raw)
    if value.get('command_sha256', fingerprint).lower() != fingerprint:
        raise ValueError('命令文件SHA256已变化；未执行')
    command = json_bytes(raw)
    if not isinstance(command, dict) or 'command_file' in command:
        raise ValueError('命令文件须包含单个阶段对象，不允许嵌套文件入口或命令列表')
    return command, {'path': str(path), 'sha256': fingerprint}


def read_request(base, step, tool_validator=None):
    validate(step, STEP, '请求步骤')
    request_path = local_path(base, step['request'])
    target = local_path(base, step['output'])
    if target.exists() or request_path == target:
        raise ValueError('请求输出已存在或与输入相同；不覆盖回执')
    request = read_object(request_path)
    validate(request, object_schema({'name': {'type': 'string', 'pattern': '^e10_[a-z0-9_]+$'},
        'arguments': {'type': 'object'}}, ['name']), 'MCP请求')
    request.setdefault('arguments', {})
    if tool_validator:
        tool_validator(request['name'], request['arguments'])
    return request, target


def prepare_reads(command, base, tool_validator=None):
    steps = command.get('steps')
    validate({'job': 'read_sequence', 'steps': steps}, command_schema({'job': 'read_sequence'}), '只读批次')
    prepared, targets, sources = [], set(), set()
    for step in steps:
        request, target = read_request(base, step, tool_validator)
        if request['name'] not in READ_TOOLS or target in targets:
            raise ValueError('只读批次含非只读工具或重复输出；未调用任何工具')
        targets.add(target)
        sources.add(local_path(base, step['request']))
        prepared.append((request, target))
    if sources & targets:
        raise ValueError('只读批次输出与请求文件相交')
    return prepared


def validate_command(command, base):
    validate(command, command_schema(command), '会话命令')
    job = command.get('job')
    inputs = {'config', 'pending', 'scope', 'source_check', 'recovery'}
    for field in inputs & command.keys():
        path = local_path(base, command[field])
        if not path.is_file():
            raise ValueError('缺少命令输入文件：' + field)
    for field in {'directory', 'checkpoint', 'receipts', 'pages', 'output'} & command.keys():
        local_path(base, command[field])
    if job == 'collect':
        filters = command['filters']
        if filters.get('date_from'):
            date.fromisoformat(filters['date_from'])
        if filters.get('workorder_no') and not re.fullmatch(r'[A-Za-z0-9]+-[A-Za-z0-9]+', filters['workorder_no']):
            raise ValueError('workorder_no仅接受单个完整工单；不能传分号串冒充列表查询')
        if filters.get('workorder_nos'):
            values = filters['workorder_nos']
            if filters.get('workorder_no'):
                raise ValueError('workorder_nos与workorder_no不能同时使用')
            if len(values) != len(set(values)) or any(not re.fullmatch(r'[A-Za-z0-9]+-[A-Za-z0-9]+', value) for value in values):
                raise ValueError('workorder_nos须为唯一的完整工单号列表')
        elif 'workorder_nos' in filters:
            raise ValueError('workorder_nos不能为空')
        directory, output = local_path(base, command['pages']), local_path(base, command['output'])
        if output.exists() or directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
            raise ValueError('查询输出或分页目录已有结果；不混用分页')
        if output == directory or output.is_relative_to(directory):
            raise ValueError('汇总输出不能放在分页目录内')
    return command


def required_tools(command):
    job = command.get('job')
    if job == 'collect':
        return ['e10_query_picking_workflow']
    if job == 'approve':
        return ['e10_prepare_document', 'e10_commit_operation']
    if job == 'workorders':
        return {'prepare': ['e10_query_order_workorder_sources', 'e10_prepare_packaging_order_batch'],
            'create': ['e10_commit_operation'], 'collect_approval': ['e10_query_workorder_workflow'],
            'approve': ['e10_prepare_workorder_confirmation_batch', 'e10_commit_operation'],
            'resume_approval': ['e10_query_workorder_workflow'],
            'approve_continuation': ['e10_prepare_workorder_confirmation_batch', 'e10_commit_operation'],
            'reconcile_prepare': ['e10_operation_status', 'e10_discard_operation'],
            'resume_prepare': ['e10_operation_status', 'e10_discard_operation']}.get(command['phase'], [])
    if job == 'manual_prepare':
        return ['e10_preview_manual_workorder', 'e10_prepare_manual_workorder']
    if job == 'manual_create':
        return ['e10_commit_operation']
    return []


def validate_capabilities(command, catalog):
    catalog.require(required_tools(command))
    if command.get('job') == 'collect':
        if 'workorder_nos' in command['filters']:
            schema = catalog.schemas.get('e10_query_picking_workflow', {})
            if 'workorder_nos' not in schema.get('properties', {}):
                raise ValueError('当前MCP查询schema不支持workorder_nos列表筛选；未删除筛选条件或发起查询')
        catalog.validate('e10_query_picking_workflow', {
            **command['filters'], 'include_detail': command.get('include_detail', True),
            'start_row': 1, 'max_rows': 1000})


class ToolCatalog:
    """One complete metadata listing per session; never logs arguments or credentials."""
    def __init__(self, schemas):
        self.schemas = copy.deepcopy(schemas)
        for schema in self.schemas.values():
            Draft202012Validator.check_schema(schema)
            self._local_refs_only(schema)

    @classmethod
    async def load(cls, session):
        schemas, cursors, cursor = {}, set(), None
        while True:
            page = await session.list_tools(**({'cursor': cursor} if cursor else {}))
            for tool in page.tools:
                if tool.name in schemas:
                    raise ValueError('工具目录出现重复名称')
                schemas[tool.name] = tool.inputSchema
            cursor = getattr(page, 'nextCursor', None)
            if not cursor:
                break
            if cursor in cursors:
                raise ValueError('工具目录分页游标重复，能力清单不完整')
            cursors.add(cursor)
        if not schemas:
            raise ValueError('工具目录为空，无法核对能力')
        return cls(schemas)

    @staticmethod
    def _local_refs_only(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('$ref', '$dynamicRef') and (not isinstance(item, str) or not item.startswith('#')):
                    raise ValueError('工具schema包含外部引用；不发起额外网络访问')
                ToolCatalog._local_refs_only(item)
        elif isinstance(value, list):
            for item in value:
                ToolCatalog._local_refs_only(item)

    def require(self, names):
        missing = [name for name in names if name not in self.schemas]
        if missing:
            raise ValueError('当前MCP缺少工具：' + ','.join(missing))

    def validate(self, name, arguments):
        self.require([name])
        schema = self.schemas[name]
        if schema.get('type') == 'object' and 'properties' in schema and 'additionalProperties' not in schema:
            schema = {**schema, 'additionalProperties': False}
        validate(arguments, schema, '工具参数：' + name)


def main():
    parser = argparse.ArgumentParser(description='Offline command validation; never starts MCP or logs in.')
    parser.add_argument('--base', required=True)
    parser.add_argument('--command-file', required=True)
    parser.add_argument('--capabilities')
    args = parser.parse_args()
    try:
        command, proof = resolve_command({'command_file': args.command_file}, Path(args.base).resolve())
        validate_command(command, args.base)
        catalog = ToolCatalog(read_object(args.capabilities)) if args.capabilities else None
        checker = catalog.validate if catalog else None
        if command.get('job') == 'read_sequence':
            prepare_reads(command, args.base, checker)
        elif 'request' in command:
            read_request(args.base, command, checker)
        if catalog:
            validate_capabilities(command, catalog)
        print(json.dumps({'ok': True, 'offline': True, 'command_file': proof['path'],
            'command_sha256': proof['sha256'], 'tool_capabilities_checked': bool(catalog)}, ensure_ascii=False))
    except (OSError, ValueError) as error:
        print(json.dumps({'ok': False, 'offline': True, 'error': str(error)}, ensure_ascii=False))
        raise SystemExit(2)


if __name__ == '__main__':
    main()
