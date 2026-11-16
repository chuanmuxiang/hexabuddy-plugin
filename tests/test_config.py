"""hexabuddy-workspace/hooks/config.py and common.sh: one precedence (environment → state.json → connection.json); ~/.hexabuddy is never read."""
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN = Path(__file__).parents[1] / 'hexabuddy-workspace'
spec = importlib.util.spec_from_file_location('buddy_config_test', PLUGIN / 'hooks/config.py')
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)

BUNDLED = json.loads((PLUGIN / 'connection.json').read_text())
KEY = 'hb_desktop_' + 'e' * 64


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.state_home = self.home / 'Library/Application Support/Hexabuddy'
        sentinel = self.home / '.hexabuddy/settings.json'
        sentinel.parent.mkdir()
        sentinel.write_text(json.dumps({'server': 'https://wrong.example', 'desktopKey': 'keychain', 'token': 'secret'}))
        self.env = {'HOME': str(self.home), 'PATH': os.environ.get('PATH', '/usr/bin:/bin')}

    def tearDown(self):
        self.directory.cleanup()

    def connection(self, **env):
        with patch.dict(os.environ, dict(self.env, **env), clear=True):
            return config.connection(), config.home()

    def test_bundled_profile_when_not_connected(self):
        conn, home = self.connection()
        self.assertEqual(home, self.state_home)
        self.assertEqual(conn['server'], BUNDLED['server'])
        self.assertEqual(conn['ca_cert'], str(PLUGIN / BUNDLED['caCertificate']))
        self.assertFalse(conn['connected'])

    def test_state_then_environment(self):
        self.state_home.mkdir(parents=True)
        (self.state_home / 'state.json').write_text(json.dumps({'server': 'https://saved.example', 'desktopId': 'a' * 32}))
        (self.state_home / 'server.pem').write_text('pem')
        conn, _ = self.connection()
        self.assertEqual((conn['server'], conn['ca_cert'], conn['connected']),
                         ('https://saved.example', str(self.state_home / 'server.pem'), True))
        conn, _ = self.connection(HEXABUDDY_SERVER='http://127.0.0.1:8787')
        self.assertEqual((conn['server'], conn['ca_cert'], conn['connected']), ('http://127.0.0.1:8787', '', False))

    def test_keychain_read_only_when_connected(self):
        with patch.object(config.credentials, 'keychain', return_value=KEY) as keychain:
            conn, _ = self.connection()
            self.assertEqual(config.credential(conn), '')
            keychain.assert_not_called()
            self.state_home.mkdir(parents=True)
            (self.state_home / 'state.json').write_text(json.dumps({'server': 'https://saved.example'}))
            conn, _ = self.connection()
            self.assertEqual(config.credential(conn), KEY)
            keychain.assert_called_once_with('https://saved.example')

    def test_environment_key_only_over_https_or_loopback(self):
        with patch.dict(os.environ, dict(self.env, HEXABUDDY_API_KEY=KEY), clear=True):
            self.assertEqual(config.credential({'server': 'http://127.0.0.1:8787', 'connected': False}), KEY)
            with self.assertRaisesRegex(RuntimeError, 'HTTPS'):
                config.credential({'server': 'http://remote.example', 'connected': False})

    def run_hook(self, **env):
        payload = json.dumps({'session_id': 's1', 'hook_event_name': 'Stop'})
        return subprocess.run(['bash', str(PLUGIN / 'hooks/report-status.sh')], input=payload, text=True,
                              capture_output=True, env=dict(self.env, **env), timeout=30)

    def test_hook_does_nothing_when_not_connected(self):
        result = self.run_hook(HEXABUDDY_HOME=str(self.state_home))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.assertFalse(self.state_home.exists())

    def test_shell_reader_output(self):
        result = subprocess.run(['python3', str(PLUGIN / 'hooks/config.py'), 'shell'], text=True, capture_output=True,
                                env=dict(self.env, HEXABUDDY_SERVER='http://127.0.0.1:8787', HEXABUDDY_API_KEY=KEY))
        self.assertEqual(result.stdout.splitlines(), [str(self.state_home), 'http://127.0.0.1:8787', '', KEY])


if __name__ == '__main__':
    unittest.main()
