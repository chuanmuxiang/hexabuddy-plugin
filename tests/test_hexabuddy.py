"""hexabuddy-workspace/scripts/hexabuddy.py: connect · disconnect · status end to end: temp HOME, stubbed Keychain, stubbed claude/codex/security, stubbed server."""
import importlib.util
import io
import json
import os
import shlex
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import urlsplit

try:
    import tomllib  # Python 3.11+; the config.toml assertions are skipped on 3.9
except ModuleNotFoundError:
    tomllib = None

spec = importlib.util.spec_from_file_location('buddy_setup', Path(__file__).parents[1] / 'hexabuddy-workspace/scripts/hexabuddy.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

KEY = 'hb_desktop_' + 'f' * 64
DESKTOP_ID = 'a' * 32
SERVER = json.loads((setup.PLUGIN / 'connection.json').read_text())['server']
HOST = urlsplit(SERVER).hostname
BUNDLED_CA = setup.PLUGIN / 'certificates/server.pem'
OTHER_HOOK = {'type': 'command', 'command': 'echo other-tool'}


class FakeMac:
    """The outside world: claude/codex/security CLIs, Keychain, server, ps."""

    def __init__(self, home):
        self.home = home
        self.keychain = {}
        self.commands = []
        self.server_calls = []
        self.claude_mcp = None
        self.codex_mcp = None
        self.signed_in = set()
        self.plugin = False
        self.trusted_host = None  # host the CA is trusted for; '*' = every host (before host scoping)
        self.codex_app = False
        self.revoke_reply = {'ok': True, 'revoked': 2, 'scopes': ['desktop', 'mcp']}
        self.approving = False
        self.pairings = 0

    @property
    def trusted(self):
        return self.trusted_host is not None

    def execute(self, command, env=None):
        self.commands.append(command)
        if command[:3] == ['claude', 'mcp', 'get']:
            if not self.claude_mcp:
                return 1, ''
            status = '✓ Connected' if 'claude' in self.signed_in else '! Needs authentication'
            return 0, f'hexabuddy-buddy:\n  Status: {status}\n  URL: {self.claude_mcp["url"]}\n'
        if command[:3] == ['claude', 'mcp', 'add']:
            self.claude_mcp = {'url': command[-1], 'client': command[command.index('--client-id') + 1]}
        if command[:3] == ['claude', 'mcp', 'remove']:
            self.claude_mcp = None
            self.signed_in.discard('claude')
        if command[:3] == ['codex', 'mcp', 'remove']:
            self.codex_mcp = None
            self.signed_in.discard('codex')
        if command[:3] == ['codex', 'mcp', 'get']:
            return (0, json.dumps({'transport': {'type': 'streamable_http', 'url': self.codex_mcp['url']}})) if self.codex_mcp else (1, '')
        if command[:3] == ['codex', 'mcp', 'list']:
            entries = [{'name': 'hexabuddy-buddy', 'auth_status': 'o_auth' if 'codex' in self.signed_in else 'not_logged_in'}] if self.codex_mcp else []
            return 0, json.dumps(entries)
        if command[:4] == ['claude', 'plugin', 'marketplace', 'list']:
            return 0, json.dumps([{'name': 'hexabuddy-plugin', 'path': str(setup.PLUGIN.parent)}] if self.plugin else [])
        if command[:3] == ['claude', 'plugin', 'list']:
            other = [{'id': 'workspace@another-marketplace', 'enabled': True}]  # not ours: never touched
            return 0, json.dumps(other + ([{'id': 'hexabuddy-workspace@hexabuddy-plugin', 'enabled': True, 'version': '0.0.5'}] if self.plugin else []))
        if command[:3] == ['claude', 'plugin', 'install']:
            self.plugin = True
        if command[:3] == ['claude', 'plugin', 'uninstall']:
            self.plugin = False
        if command[:2] == ['security', 'verify-cert']:
            return (0 if self.trusted_host in ('*', command[command.index('-s') + 1]) else 1), ''
        if command[:2] == ['security', 'delete-certificate']:
            self.trusted_host = None
        if command[0] == 'bash' and command[1].endswith('translate-to-buddy/install.sh'):
            (self.home / 'Library/Services/Translate to Buddy.workflow').mkdir(parents=True, exist_ok=True)
        if command[0] == 'bash' and command[1].endswith('translate-to-buddy/uninstall.sh'):
            os.rmdir(self.home / 'Library/Services/Translate to Buddy.workflow')
        if command[0] == 'ps':
            return 0, '/Applications/ChatGPT.app/Contents/Frameworks/Codex Framework.framework/Versions/A/Codex (Aperitif).app/x\n' if self.codex_app else '/bin/zsh\n'
        return 0, ''

    def interactive(self, command, env=None):
        self.commands.append(command)
        if command[:3] == ['codex', 'mcp', 'add']:
            self.codex_mcp = {'url': command[command.index('--url') + 1], 'client': command[command.index('--oauth-client-id') + 1]}
            assert env.get('CODEX_CA_CERTIFICATE'), 'codex add trusts the server CA'
        if command[1:3] == ['mcp', 'login']:
            assert self.approving, 'sign-in runs while the Mac approves it'
            self.signed_in.add(command[0])
        if command[:2] == ['security', 'add-trusted-cert']:
            self.trusted_host = command[command.index('-s') + 1] if '-s' in command else '*'

    def keychain_call(self, server, value=None, *, delete=False):
        if delete:
            self.keychain.pop(server, None)
            return None
        if value is None:
            return self.keychain.get(server, '')
        self.keychain[server] = value

    def post_json(self, server, path, body, ca_cert=None, api_key=None):
        self.server_calls.append((path, api_key))
        if isinstance(self.revoke_reply, Exception):
            raise self.revoke_reply
        return self.revoke_reply

    def pair(self, server, name, ca_cert=None, desktop_id=None):
        self.pairings += 1
        return KEY, DESKTOP_ID

    @contextmanager
    def approval(self, server, api_key, ca_cert=None):
        self.approving = True
        try:
            yield
        finally:
            self.approving = False


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.codex = self.home / '.codex'
        self.codex.mkdir()
        self.state_home = self.home / 'Library/Application Support/Hexabuddy'
        # A developer file the plugin must never read or write.
        self.sentinel = self.home / '.hexabuddy/settings.json'
        self.sentinel.parent.mkdir()
        self.sentinel.write_text(json.dumps({'server': 'https://wrong.example', 'desktopKey': 'keychain', 'token': 'secret'}))
        self.sentinel_bytes = self.sentinel.read_bytes()
        (self.codex / 'hooks.json').write_text(json.dumps({'hooks': {'Stop': [{'hooks': [OTHER_HOOK]}]}}))
        self.mac = FakeMac(self.home)

    def tearDown(self):
        self.assertEqual(self.sentinel.read_bytes(), self.sentinel_bytes)
        self.assertEqual(sorted(p.name for p in self.sentinel.parent.iterdir()), ['settings.json'])
        self.directory.cleanup()

    def state(self):
        return json.loads((self.state_home / 'state.json').read_text())

    def run_command(self, *argv, which=('claude', 'codex', 'jq')):
        env = {'HOME': str(self.home), 'HEXABUDDY_HOME': str(self.state_home), 'CODEX_HOME': str(self.codex),
               'CLAUDE_CONFIG_DIR': str(self.home / '.claude'), 'PATH': '/usr/bin:/bin'}
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), \
             patch.object(setup.Path, 'home', return_value=self.home), \
             patch.object(setup.shutil, 'which', side_effect=lambda name: '/stub/' + name if name in which else None), \
             patch.object(setup, 'execute', side_effect=self.mac.execute), \
             patch.object(setup, 'run_interactive', side_effect=self.mac.interactive), \
             patch.object(setup._credentials, 'keychain', side_effect=self.mac.keychain_call), \
             patch.object(setup, 'post_json', side_effect=self.mac.post_json), \
             patch.object(setup, 'pair_desktop', side_effect=self.mac.pair), \
             patch.object(setup, 'mac_approval', side_effect=self.mac.approval), \
             patch.object(setup, 'check_remote_mcp'), \
             patch.object(setup, 'server_certificate', return_value=BUNDLED_CA.read_text()), \
             patch.object(setup, 'connection_opener', side_effect=AssertionError('no real network in tests')), \
             patch.object(setup, 'report_switches', side_effect=lambda *a: print('  Phone switches: monitor on · vocabulary off')), \
             patch.object(setup, 'key_state', side_effect=lambda server, key, ca=None: 'accepted' if key in self.mac.keychain.values() or key == KEY else 'rejected'), \
             redirect_stdout(output):
            code = setup.main(list(argv))
        return code, output.getvalue()

    def our_hooks(self):
        hooks = json.loads((self.codex / 'hooks.json').read_text())['hooks']
        return [h for groups in hooks.values() for g in groups for h in g['hooks'] if setup.is_our_hook(h)]

    def connect_and_trust(self):
        code, output = self.run_command('connect')
        ids = setup.codex_hook_ids(self.codex / 'hooks.json')
        records = ''.join(f'[hooks.state."{record}"]\ntrusted_hash = "sha256:{i}"\n\n' for i, record in enumerate(ids))
        other = f'[hooks.state."{self.codex}/hooks.json:stop:0:0"]\ntrusted_hash = "sha256:other"\n\n'
        (self.codex / 'config.toml').write_text('model = "x"\n\n[hooks.state]\n\n' + other + records + '[mcp_servers.other]\nurl = "https://o"\n')
        return code, output

    def test_connect_installs_every_piece_with_one_approval(self):
        code, output = self.run_command('connect')
        ca = str(self.state_home / 'server.pem')
        self.assertEqual(self.state(), {'server': SERVER, 'desktopId': DESKTOP_ID,
                                        'trust': {'keychain': setup.certificate_sha1(ca), 'host': HOST, 'claudeEnv': ca}})
        security = [c for c in self.mac.commands if c[0] == 'security']
        self.assertEqual(len(security), 2)
        self.assertEqual(security[0][:3] + security[0][4:], ['security', 'verify-cert', '-c', '-p', 'ssl', '-s', HOST])
        self.assertTrue(security[0][3].endswith('.pem'))  # the server's own certificate, fetched
        self.assertEqual(security[1], ['security', 'add-trusted-cert', '-r', 'trustRoot', '-p', 'ssl', '-s', HOST,
                                       '-k', str(self.home / 'Library/Keychains/login.keychain-db'), ca])
        self.assertEqual(Path(ca).read_bytes(), BUNDLED_CA.read_bytes())
        self.assertEqual(sorted(p.name for p in self.state_home.iterdir()),
                         ['hooks', 'server.pem', 'state.json', 'translate-to-buddy'])
        self.assertEqual(self.state_home.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.mac.keychain, {SERVER: KEY})
        self.assertEqual(self.mac.pairings, 1)
        self.assertEqual(json.loads((self.home / '.claude/settings.json').read_text())['env']['NODE_EXTRA_CA_CERTS'], ca)
        self.assertTrue(self.mac.trusted)
        self.assertTrue(self.mac.plugin)
        self.assertEqual(len(self.our_hooks()), 7)
        self.assertTrue((self.state_home / 'hooks/report-status.sh').exists())
        self.assertTrue(all(shlex.split(h['command'])[-1].startswith(str(self.state_home / 'hooks')) for h in self.our_hooks()))
        self.assertIn(['claude', 'plugin', 'marketplace', 'add', str(setup.PLUGIN.parent)], self.mac.commands)
        self.assertEqual(self.mac.claude_mcp, {'url': SERVER + '/mcp', 'client': 'hexabuddy-claude.' + DESKTOP_ID})
        self.assertEqual(self.mac.codex_mcp, {'url': SERVER + '/mcp', 'client': 'hexabuddy-codex.' + DESKTOP_ID})
        self.assertEqual(self.mac.signed_in, {'claude', 'codex'})
        self.assertTrue((self.home / 'Library/Services/Translate to Buddy.workflow').exists())
        self.assertIn('✓ claude hexabuddy-buddy: connected', output)
        self.assertIn('✓ codex hexabuddy-buddy: connected', output)
        self.assertIn('Phone switches: monitor on · vocabulary off', output)
        self.assertIn('✗ Codex hooks trusted: 0/7', output)  # Codex asks the person to review new hooks
        self.assertEqual(code, 1)
        self.assertNotIn(KEY, output)

    def test_connect_again_changes_nothing(self):
        self.connect_and_trust()
        hooks = (self.codex / 'hooks.json').read_text()
        self.mac.commands.clear()
        code, output = self.run_command('connect')
        self.assertEqual(code, 0, output)
        self.assertEqual(self.mac.pairings, 1)
        self.assertEqual((self.codex / 'hooks.json').read_text(), hooks)
        self.assertFalse([c for c in self.mac.commands if c[1:3] in (['mcp', 'add'], ['mcp', 'login']) or c[:2] == ['security', 'add-trusted-cert']])

    def test_disconnect_revokes_first_and_removes_every_piece(self):
        self.connect_and_trust()
        state = {'mcp-server-tools-cache-v2': [{'server': 'hexabuddy-buddy', 'tools': []}, {'server': 'other', 'tools': []}],
                 'mcp-extension-sidebar-catalog': [{'name': 'hexabuddy-buddy'}, {'name': 'docs'}], 'unrelated': 1}
        (self.codex / '.codex-global-state.json').write_text(json.dumps(state))
        self.mac.commands.clear()
        code, output = self.run_command('disconnect')
        self.assertEqual(code, 0, output)
        self.assertEqual(self.mac.server_calls, [('/desktop/revoke-self', KEY)])
        self.assertIn('✓ Server: revoked 2 key(s) of this Mac (hooks, questions)', output)
        self.assertFalse(self.state_home.exists())
        self.assertEqual(self.mac.keychain, {})
        self.assertEqual(self.our_hooks(), [])
        self.assertEqual(json.loads((self.codex / 'hooks.json').read_text())['hooks']['Stop'], [{'hooks': [OTHER_HOOK]}])
        if tomllib:
            config = tomllib.loads((self.codex / 'config.toml').read_text())
            self.assertEqual(list(config['hooks']['state']), [f'{self.codex}/hooks.json:stop:0:0'])
            self.assertEqual(config['mcp_servers'], {'other': {'url': 'https://o'}})
        self.assertTrue(list(self.codex.glob('config.toml.bak.*')))
        cache = json.loads((self.codex / '.codex-global-state.json').read_text())
        self.assertEqual(cache, {'mcp-server-tools-cache-v2': [{'server': 'other', 'tools': []}],
                                 'mcp-extension-sidebar-catalog': [{'name': 'docs'}], 'unrelated': 1})
        self.assertIsNone(self.mac.claude_mcp)
        self.assertIsNone(self.mac.codex_mcp)
        self.assertIn(['claude', 'mcp', 'logout', 'hexabuddy-buddy'], self.mac.commands)
        self.assertIn(['codex', 'mcp', 'logout', 'hexabuddy-buddy'], self.mac.commands)
        self.assertFalse(self.mac.plugin)
        self.assertNotIn(['claude', 'plugin', 'uninstall', 'workspace@another-marketplace'], self.mac.commands)
        self.assertFalse(self.mac.trusted)
        sha1 = setup.certificate_sha1(BUNDLED_CA)
        self.assertIn(['security', 'delete-certificate', '-t', '-Z', sha1, str(self.home / 'Library/Keychains/login.keychain-db')], self.mac.commands)
        self.assertIn(f'✓ Server certificate trust for {HOST} removed from the login Keychain.', output)
        self.assertNotIn('env', json.loads((self.home / '.claude/settings.json').read_text()))
        self.assertFalse((self.home / 'Library/Services/Translate to Buddy.workflow').exists())
        self.assertIn('This Mac is disconnected.', output)
        code, output = self.run_command('status')
        self.assertEqual(code, 1)
        self.assertIn('This Mac is not connected', output)
        self.assertIn('No file mentions hexabuddy-buddy.', output)

    def test_disconnect_offline_still_clears_and_points_to_the_phone(self):
        self.connect_and_trust()
        self.mac.revoke_reply = URLError('offline')
        code, output = self.run_command('disconnect')
        self.assertEqual(code, 0)
        self.assertEqual(self.mac.keychain, {})
        self.assertFalse(self.state_home.exists())
        self.assertIn('✗ Server: server unreachable', output)
        self.assertIn('Devices → this Mac → Disconnect this Mac', output)

    def test_running_codex_app_keeps_its_cache_until_rerun(self):
        self.connect_and_trust()
        cache = self.codex / '.codex-global-state.json'
        cache.write_text(json.dumps({'mcp-server-tools-cache-v2': [{'server': 'hexabuddy-buddy'}]}))
        self.mac.codex_app = True
        code, output = self.run_command('disconnect')
        self.assertEqual(code, 1)
        self.assertIn('hexabuddy-buddy', cache.read_text())
        self.assertIn('Quit ChatGPT, then run disconnect again', output)
        self.mac.codex_app = False
        code, output = self.run_command('disconnect')
        self.assertEqual(code, 0)
        self.assertNotIn('hexabuddy-buddy', cache.read_text())

    def test_status_names_a_missing_keychain_key(self):
        self.state_home.mkdir(parents=True)
        (self.state_home / 'state.json').write_text(json.dumps({'server': SERVER}))
        code, output = self.run_command('status', which=())
        self.assertEqual(code, 1)
        self.assertIn('Desktop key: missing from the Keychain', output)
        self.assertNotIn('unreachable', output)

    def test_status_without_agents_does_not_write(self):
        code, output = self.run_command('status', which=())
        self.assertEqual(code, 1)
        self.assertIn('Server: not set', output)
        self.assertFalse(self.state_home.exists())
        self.assertEqual(self.mac.server_calls, [])

    def test_disconnect_when_never_connected_touches_nothing_else(self):
        code, output = self.run_command('disconnect')
        self.assertIn('no desktop key on this Mac', output)
        self.assertFalse(self.state_home.exists())
        self.assertEqual(self.mac.keychain, {})

    def test_reconnect_to_another_server_keeps_one_keychain_item(self):
        self.connect_and_trust()
        self.run_command('connect', '--server', 'https://other.example', '--ca-cert', str(BUNDLED_CA))
        self.assertEqual(self.mac.keychain, {'https://other.example': KEY})
        self.assertEqual(self.state()['server'], 'https://other.example')
        self.assertEqual(self.mac.trusted_host, 'other.example')  # the CA follows the server host
        self.assertEqual(self.state()['trust']['host'], 'other.example')

    def test_connect_replaces_an_older_trust_for_every_host(self):
        # Before host scoping, connect trusted the CA for SSL to every host and recorded no host.
        self.state_home.mkdir(parents=True)
        sha1 = setup.certificate_sha1(BUNDLED_CA)
        (self.state_home / 'state.json').write_text(json.dumps({'server': SERVER, 'trust': {'keychain': sha1}}))
        self.mac.trusted_host = '*'
        self.run_command('connect')
        keychain = str(self.home / 'Library/Keychains/login.keychain-db')
        security = [c for c in self.mac.commands if c[0] == 'security']
        self.assertEqual(security[0], ['security', 'delete-certificate', '-t', '-Z', sha1, keychain])
        self.assertEqual(security[-1][:9], ['security', 'add-trusted-cert', '-r', 'trustRoot', '-p', 'ssl', '-s', HOST, '-k'])
        self.assertEqual(self.mac.trusted_host, HOST)
        self.assertEqual(self.state()['trust']['host'], HOST)

    def test_server_trust_check_verifies_the_server_certificate_for_its_host(self):
        commands = []
        with patch.object(setup, 'server_certificate', return_value=BUNDLED_CA.read_text()), \
             patch.object(setup, 'execute', side_effect=lambda c, env=None: (commands.append(c), (0, ''))[1]):
            self.assertTrue(setup.server_trusted('https://10.0.0.5:8443'))
        self.assertEqual(commands[0][:3] + commands[0][4:], ['security', 'verify-cert', '-c', '-p', 'ssl', '-s', '10.0.0.5'])
        with patch.object(setup, 'server_certificate', side_effect=OSError('unreachable')):
            self.assertFalse(setup.server_trusted('https://10.0.0.5:8443'))


class PairingTests(unittest.TestCase):
    def test_pairing_sends_desktop_id_returns_key_and_never_prints_secrets(self):
        replies = [{'device_code': 'a' * 64, 'user_code': 'ABCDE-12345', 'expires_in': 600},
                   {'status': 'pending'}, {'status': 'approved', 'key': KEY, 'desktopId': 'c' * 32}]
        sent = []

        def post(server, path, body, ca_cert=None, api_key=None):
            sent.append((path, body))
            return replies.pop(0)
        output = io.StringIO()
        with patch.object(setup, 'post_json', side_effect=post), patch.object(setup, 'pairing_window'), \
             patch.object(setup.time, 'sleep'), redirect_stdout(output):
            self.assertEqual(setup.pair_desktop('https://server.example', 'Mac', desktop_id='d' * 32), (KEY, 'c' * 32))
        self.assertEqual(sent[0], ('/api/desktop/pair', {'name': 'Mac', 'desktop_id': 'd' * 32}))
        self.assertIn('ABCDE-12345', output.getvalue())
        self.assertNotIn(KEY, output.getvalue())
        self.assertNotIn('a' * 64, output.getvalue())

    def test_pairing_window_never_exposes_redeeming_secret(self):
        pair = {'user_code': 'ABCDE-12345', 'device_code': 'secret-fixture', 'qr_png': 'data:image/png;base64,aW1hZ2U='}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'HEXABUDDY_HOME': directory}), \
             patch.object(setup.webbrowser, 'open', return_value=True) as browser:
            with setup.pairing_window(pair, '<untrusted name>'):
                page = next(Path(directory).glob('pair-*.html'))
                text = page.read_text()
                self.assertIn('&lt;untrusted name&gt;', text)
                self.assertNotIn('secret-fixture', text)
                self.assertEqual(page.stat().st_mode & 0o777, 0o600)
                self.assertTrue(browser.call_args.args[0].startswith('file://'))
            self.assertIn('Phone approved', page.read_text())

    def test_revoke_self_reports_unlinked_questions_key(self):
        with patch.object(setup, 'post_json', return_value={'scopes': ['desktop']}):
            ok, message = setup.revoke_self('https://server.example', KEY)
        self.assertTrue(ok)
        self.assertIn('revoke it there', message)

    def test_trust_records_read_without_tomllib(self):
        # Python 3.9 (Xcode Command Line Tools) has no tomllib; the header fallback reads the same ids.
        lines = ['[hooks.state."/u/a b/.codex/hooks.json:stop:0:0"]', "[hooks.state.'/u/x/hooks.json:pre_tool_use:1:0']",
                 '[hooks.state."/u/q\\"x/hooks.json:stop:0:0"]', '[hooks.state]', '[mcp_servers.other]']
        expected = [setup.trust_record_id(line) for line in lines]
        with patch.object(setup, 'tomllib', None):
            self.assertEqual([setup.trust_record_id(line) for line in lines], expected)
        self.assertEqual(expected[:3], ['/u/a b/.codex/hooks.json:stop:0:0', '/u/x/hooks.json:pre_tool_use:1:0', '/u/q"x/hooks.json:stop:0:0'])

    def test_reject_invalid_server(self):
        for value in ['file:///tmp/key', 'https://user:secret@server.example', 'https://server.example?token=x']:
            with self.assertRaises(setup.SetupError):
                setup.server_url(value)
        with self.assertRaises(setup.SetupError):
            setup.secure_key_url('http://public.example')


if __name__ == '__main__':
    unittest.main()


class InteractiveCommandTest(unittest.TestCase):
    # /hexabuddy-workspace:connect runs without a terminal; `claude mcp login` needs one (2026-11-16 live failure).
    def test_no_terminal_gets_a_pseudo_terminal(self):
        with patch.object(setup.sys.stdin, 'isatty', return_value=False), \
                patch.object(setup.shutil, 'which', return_value='/usr/bin/script'):
            self.assertEqual(setup.interactive_command(['claude', 'mcp', 'login', 'hexabuddy-buddy']),
                             ['script', '-q', '/dev/null', 'claude', 'mcp', 'login', 'hexabuddy-buddy'])

    def test_terminal_runs_the_command_directly(self):
        with patch.object(setup.sys.stdin, 'isatty', return_value=True):
            self.assertEqual(setup.interactive_command(['claude', 'mcp', 'login', 'x']), ['claude', 'mcp', 'login', 'x'])

    def test_failure_still_reported(self):
        with patch.object(setup.sys.stdin, 'isatty', return_value=False), \
                patch.object(setup.subprocess, 'run', return_value=type('R', (), {'returncode': 1})()) as run:
            with self.assertRaises(setup.SetupError):
                setup.run_interactive(['claude', 'mcp', 'login', 'hexabuddy-buddy'])
            self.assertEqual(run.call_args[0][0][:3], ['script', '-q', '/dev/null'])

