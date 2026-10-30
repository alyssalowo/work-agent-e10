"""Exercise generated local settings and conflict protection without ERP or pip."""
import importlib.util
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('portable_install', REPO / 'mcp/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'workspace with spaces'
        self.server = self.root / 'mcp/e10'
        self.server.mkdir(parents=True)
        self.client = self.root / 'client'
        for dll in installer.REQUIRED_DLLS:
            path = self.client / dll
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        for name in ['connection.example.json', 'codex-mcp.example.toml']:
            (self.server / name).write_bytes((REPO / 'mcp/e10' / name).read_bytes())
        for name, value in [('ROOT', self.root), ('SERVER', self.server)]:
            context = patch.object(installer, name, value)
            context.start()
            self.addCleanup(context.stop)
        self.argv = ['install.py', '--client-directory', str(self.client)]

    def run_installer(self, command=None):
        command = command if command is not None else Mock()
        with patch.object(installer.sys, 'argv', self.argv), \
             patch.object(installer.sys, 'platform', 'win32'), \
             patch.object(installer.sys, 'version_info', (3, 12, 14)), \
             patch.object(installer.subprocess, 'run', command):
            installer.main()
            return command

    def test_generate_paths_repeat_and_conflict(self):
        self.run_installer()
        config_path = self.root / '.codex/config.toml'
        content = config_path.read_bytes()
        config = tomllib.loads(content.decode())['mcp_servers']['dingjie-e10-account']
        self.assertEqual(config['cwd'], self.server.as_posix())
        self.assertEqual(config['tool_timeout_sec'], 360)
        connection = json.loads((self.server / 'connection.json').read_text(encoding='utf-8'))
        self.assertEqual(connection['client_directory'], str(self.client))
        self.run_installer()
        self.assertEqual(content, config_path.read_bytes())
        config_path.write_text('[mcp_servers.existing]\ncommand="other"\n', encoding='utf-8')
        with patch.object(installer.subprocess, 'run') as command:
            with self.assertRaises(ValueError):
                self.run_installer(command)
            command.assert_not_called()
        self.assertIn('existing', config_path.read_text())

    def test_missing_client_stops_without_local_configuration(self):
        (self.client / installer.REQUIRED_DLLS[0]).unlink()
        with self.assertRaises(SystemExit):
            self.run_installer()
        self.assertFalse((self.server / 'connection.json').exists())
        self.assertFalse((self.root / '.codex/config.toml').exists())


if __name__ == '__main__':
    unittest.main()
