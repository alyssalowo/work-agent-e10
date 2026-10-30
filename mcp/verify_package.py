"""Offline MCP protocol and package integrity check; never log in or write ERP."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent / 'e10'


async def main():
    manifest = json.loads((ROOT / 'manifest.sha256.json').read_text(encoding='utf-8'))
    for name, expected in manifest.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
    sys.path.insert(0, str(ROOT))
    import manual_library
    catalog = manual_library.catalog()
    images = 0
    for entry in catalog['documents']:
        doc = manual_library.document(entry['manual_id'])
        for img in doc['images']:
            assert manual_library.image_path(entry['manual_id'], img['image_id']).is_file()
            images += 1
    # A temporary configuration permits initialization without touching the live install.
    # The PowerShell worker starts only on ERP tools; this check calls manual tools only.
    with tempfile.TemporaryDirectory(prefix='e10-package-check-') as temp:
        temp_root = Path(temp)
        shutil.copy2(ROOT / 'server.py', temp_root)
        shutil.copy2(ROOT / 'manual_library.py', temp_root)
        shutil.copytree(ROOT / 'manuals', temp_root / 'manuals')
        shutil.copy2(ROOT / 'connection.example.json', temp_root / 'connection.json')
        parameters = StdioServerParameters(command=sys.executable, args=[str(temp_root / 'server.py')],
                                           cwd=str(temp_root), env={**os.environ, 'PYTHONUTF8': '1'})
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                assert {'e10_shipment_query_configuration', 'e10_query_shipment_details',
                        'e10_commit_operation', 'e10_read_manual'} <= names
                for name, args in [('e10_read_manual', {'manual_id': 'integration-guide'}),
                                   ('e10_search_manual', {'query': '工单'})]:
                    result = await session.call_tool(name, args)
                    assert not result.isError, result
                print(json.dumps({'manifest_files': len(manifest), 'tools': len(names),
                                  'manuals': len(catalog['documents']), 'images': images,
                                  'mcp_initialize': True, 'manual_read_search': True,
                                  'erp_login_or_write': False}, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
