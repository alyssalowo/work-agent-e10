"""Install this repository's E10 MCP on Windows without ERP business calls."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / 'mcp' / 'e10'
REQUIRED_DLLS = [
    'System/Digiwin.Mars.Common.dll', 'System/Digiwin.Mars.Common.Advanced.dll',
    'System/Digiwin.Mars.Core.dll', 'System/Digiwin.Mars.Network.dll',
    'System/Digiwin.Mars.AccountSets.dll', 'System/Digiwin.Mars.Business.dll',
    'System/Digiwin.Mars.Business.Utils.dll', 'System/Digiwin.Mars.ApplicationUtils.Common.dll',
    'System/Digiwin.Mars.Torridity.dll', 'System/Digiwin.Mars.Utils.dll',
    'Programs/Digiwin.ERP.MO_B002.Business.dll', 'Programs/Digiwin.ERP.Common.Business.dll',
    'Programs/Digiwin.ERP.GenerateIssueReturnService.Business.dll',
    'Programs/Digiwin.ERP.MO.Business.dll', 'Programs/Digiwin.ERP.CommonManufacture.Business.dll',
]


def write_new_or_equal(path: Path, text: str) -> None:
    if path.exists():
        if path.read_text(encoding='utf-8-sig') != text:
            raise ValueError(f'已有不同配置，未覆盖，请先备份并手动合并：{path}')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8', newline='\n') as handle:
        handle.write(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-directory', type=Path,
                        default=Path(os.environ.get('LOCALAPPDATA', '')) / 'Digiwin/E10/Client')
    parser.add_argument('--server-url', default='http://192.168.0.2:8002/WCFGeneralService')
    parser.add_argument('--account-set-id', default='DemoCompany')
    parser.add_argument('--account-set-name', default='大寰公司')
    parser.add_argument('--check', action='store_true', help='仅检查已安装文件，不安装、不连接 ERP')
    args = parser.parse_args()
    if sys.platform != 'win32' or sys.version_info[:2] != (3, 12):
        parser.error('请使用 Windows 上的 Python 3.12：py -3.12 mcp/install.py')
    client = args.client_directory.resolve()
    missing = [name for name in REQUIRED_DLLS if not (client / name).is_file()]
    if missing:
        parser.error('E10 客户端目录缺少组件：' + ', '.join(missing))
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    if not powershell.is_file():
        parser.error('缺少 Windows PowerShell 5.1')
    python = SERVER / '.venv/Scripts/python.exe'
    connection = json.loads((SERVER / 'connection.example.json').read_text(encoding='utf-8'))
    connection.update(client_directory=str(client), server_url=args.server_url,
                      account_set_id=args.account_set_id, account_set_name=args.account_set_name)
    config = {'mcp_servers': {'dingjie-e10-account': {
        'enabled': True, 'command': python.as_posix(), 'args': [(SERVER / 'server.py').as_posix()],
        'cwd': SERVER.as_posix(), 'startup_timeout_sec': 30, 'tool_timeout_sec': 360,
        'env': {'PYTHONUTF8': '1'},
    }}}
    snippet = (SERVER / 'codex-mcp.example.toml').read_text(encoding='utf-8')
    snippet = snippet.replace('__E10_ROOT__', SERVER.as_posix())
    assert tomllib.loads(snippet) == config
    connection_text = json.dumps(connection, ensure_ascii=False, indent=2) + '\n'
    config_path = ROOT / '.codex/config.toml'
    connection_path = SERVER / 'connection.json'
    # Refuse conflicting local settings before installing dependencies or writing either file.
    for path, wanted in [(connection_path, connection_text), (config_path, snippet)]:
        if path.exists() and path.read_text(encoding='utf-8-sig') != wanted:
            raise ValueError(f'已有不同配置，未覆盖，请先备份并手动合并：{path}')
    if args.check:
        if not python.is_file() or not connection_path.is_file() or not config_path.is_file():
            parser.error('安装尚未完成，请先不带 --check 运行')
        subprocess.run([str(python), '-m', 'pip', 'check'], check=True)
        print('本机文件和依赖检查通过；尚未验证账号或 ERP 网络。')
        return
    if not python.exists():
        subprocess.run([sys.executable, '-m', 'venv', str(SERVER / '.venv')], check=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(SERVER / 'requirements.lock.txt')], check=True)
    subprocess.run([str(python), '-m', 'pip', 'check'], check=True)
    write_new_or_equal(connection_path, connection_text)
    write_new_or_equal(config_path, snippet)
    print('安装完成。下一步运行 mcp/02-configure-account.cmd 保存本机账号，再重开 Codex 并信任项目。')


if __name__ == '__main__':
    main()
