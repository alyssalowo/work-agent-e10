"""Local-only startup checks. Never load credentials, initialize MCP or log in."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from local_evidence import run_subprocess


def inspect_runtime(server, python=None, identity_probe=None):
    server = Path(server).resolve()
    root = server.parent
    checks = []

    def add(name, ok, remedy=None):
        checks.append({'check': name, 'ok': bool(ok), **({'remedy': remedy} if not ok else {})})

    expected_python = Path(python or root / '.venv/Scripts/python.exe').resolve()
    add('configured_python', os.path.normcase(str(Path(sys.executable).resolve())) == os.path.normcase(str(expected_python)),
        '使用当前E10 MCP配置中的Python解释器启动助手。')
    files = ['server.py', 'connection.json', 'connection-worker.ps1', 'load-client.ps1',
             'E10OrderBatch.cs', 'credentials.dpapi']
    for name in files:
        add('file:' + name, (root / name).is_file(), '修复本机集成文件后再启动；不要登录测试。')
    add('mcp_sdk', importlib.util.find_spec('mcp') is not None, '使用含MCP SDK的配置解释器。')
    add('jsonschema', importlib.util.find_spec('jsonschema') is not None,
        '配置解释器须包含jsonschema，以便调用前核对真实MCP参数；不登录排查。')
    for path in Path(__file__).parent.glob('*.py'):
        try:
            ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
            add('syntax:' + path.name, True)
        except (SyntaxError, OSError, UnicodeError):
            add('syntax:' + path.name, False, '先修复本地脚本并完成离线回归。')
    if (root / 'connection.json').is_file():
        try:
            config = json.loads((root / 'connection.json').read_text(encoding='utf-8-sig'))
            client = Path(config['client_directory'])
            for relative in ['System/Digiwin.Mars.Common.dll', 'System/Digiwin.Mars.Common.Advanced.dll',
                             'Programs/Digiwin.ERP.MO_B002.Business.dll',
                             'Programs/Digiwin.ERP.GenerateIssueReturnService.Business.dll']:
                add('client:' + relative, (client / relative).is_file(), '修复当前配置的E10客户端依赖。')
        except (OSError, ValueError, KeyError, TypeError):
            add('client_configuration', False, '检查本机connection.json配置；不打印配置或凭据内容。')
    credential = root / 'credentials.dpapi'
    if credential.is_file():
        try:
            if identity_probe is not None:
                identity = identity_probe(credential)
            elif os.name == 'nt':
                powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
                result = run_subprocess([str(powershell), '-NoProfile', '-NonInteractive', '-File',
                    str(Path(__file__).with_name('runtime_identity.ps1')), '-CredentialPath', str(credential)],
                    capture_output=True, timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW, check=True)
                identity = json.loads(result.stdout.decode('utf-8-sig'))
            else:
                identity = {'same_user': False}
            add('credential_user_context', identity.get('same_user') is True,
                '当前运行用户与凭据文件所有者不同；在保存凭据的Windows用户环境恢复助手，勿尝试登录或复制凭据。')
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            add('credential_user_context', False, '无法核实Windows用户上下文；先解决本机权限，不尝试登录。')
    try:
        spec = importlib.util.spec_from_file_location('_packing_preflight_data', Path(__file__).with_name('workflow_data.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        header = ['单号','单身来源单号','销货明细_品号','销货明细_特征码','销货明细_业务数量','包装工单','物料领料单号','机器领料单号']
        identity = dict(zip(header[:6], ['SYNTHETIC','ORDER','1-TEST','','1','5101-TEST']))
        row = [*identity.values(), '', '']
        synthetic = {'schema_version':1,'phase':'collect','ok':True,'target_url':'https://example.invalid/preflight',
                     'sheets':[{'name':'check','range':'A1:H3','tsv':'\t'.join(header)+'\n'+'\t'.join(row)+'\n'+'\t'.join([*row[:5],'5101-KEEP','',''])+'\n'}]}
        exclusions = {'schema_version':1,'target_url':synthetic['target_url'],'reason':'offline contract test',
                      'rows':[{'sheet':'check','row':2,'identity':identity}]}
        result = module.extract_orders(synthetic, exclusions=exclusions)
        if result['orders'] != ['5101-KEEP'] or len(result['excluded_rows']) != 1:
            raise ValueError('Adapter contract mismatch')
        add('ordinary_sheet_alias_and_exclusion_contract', True)
    except (OSError, ImportError, ValueError, TypeError, KeyError):
        add('ordinary_sheet_alias_and_exclusion_contract', False, '先修复字段/排除适配并运行离线回归，勿先开始E10业务。')
    return {'schema_version': 1, 'ok': all(x['ok'] for x in checks), 'phase': 'local_preflight',
            'offline': True, 'credentials_read': False, 'authenticated': False,
            'checks': checks, 'failed': [x for x in checks if not x['ok']]}
