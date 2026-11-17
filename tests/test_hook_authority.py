"""S-05 on the Mac: text the plugin hooks carry (prompts, tool inputs, replies, server answers) is data, never an action.

report-status.sh runs against a loopback stub server with the desktop key from the environment. Shell syntax in a
prompt, a tool command or a session id must never run. No permission hook ships (permission prompts stay in the
terminal, 2026-11-17). No real server, Keychain or ~/.hexabuddy is touched.
"""
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOOKS = Path(__file__).parents[1] / 'hexabuddy-workspace/hooks'
KEY = 'hb_desktop_' + 'c' * 64


class Stub(BaseHTTPRequestHandler):
    posts = []
    gets = []
    decision = {'decision': 'allow'}

    def log_message(self, *a):
        pass

    def reply(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        Stub.gets.append(self.path)
        if self.path.endswith('/watching'):
            return self.reply({'watching': True})
        if '/approval/' in self.path:
            return self.reply(Stub.decision if self.path.split('/approval/')[1].startswith('req-1') else {'decision': 'allow'})
        self.reply({})

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n).decode()
        Stub.posts.append((self.path, body))
        if self.path.endswith('/approval-request'):
            return self.reply({'request_id': 'req-1'})
        self.reply({'ok': True})


class HookAuthorityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Stub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = 'http://127.0.0.1:%d' % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Stub.posts.clear(); Stub.gets.clear(); Stub.decision = {'decision': 'allow'}
        self.dir = tempfile.TemporaryDirectory(); self.home = Path(self.dir.name)
        self.pwn = self.home / 'PWNED'
        self.project = self.home / 'project'; self.project.mkdir()
        self.env = {'HOME': str(self.home), 'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                    'HEXABUDDY_SERVER': self.url, 'HEXABUDDY_API_KEY': KEY, 'HEXABUDDY_APPROVE_LOG': '',
                    'CLAUDE_PROJECT_DIR': str(self.project)}
        p = str(self.pwn)
        self.evil = [f'$(touch {p})', f'`touch {p}`', f'; touch {p}', f'" && touch {p} && echo "', f"' ; touch {p} ; '",
                     f'$((1)) ${{IFS}}$(touch {p})', f'\n touch {p}\n']

    def tearDown(self):
        self.dir.cleanup()

    def hook(self, name, payload):
        return subprocess.run(['bash', str(HOOKS / name)], input=json.dumps(payload), text=True, capture_output=True,
                              env=self.env, cwd=self.project, timeout=120)

    def wait_posts(self, n, seconds=10):
        end = time.time() + seconds
        while len(Stub.posts) < n and time.time() < end:
            time.sleep(0.05)

    def test_status_events_carry_shell_text_as_data(self):
        for i, text in enumerate(self.evil):
            r = self.hook('report-status.sh', {'session_id': 's1', 'hook_event_name': 'UserPromptSubmit', 'prompt': text,
                                               'tool_name': text, 'model': text})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.wait_posts(i + 1)
        time.sleep(0.5)
        self.assertFalse(self.pwn.exists(), 'shell text in a prompt or tool name ran')
        bodies = [json.loads(b) for _, b in Stub.posts]
        self.assertEqual([b['detail'] for b in bodies], [t[:4000].rstrip('\n') for t in self.evil])   # verbatim as data ($(…) trims trailing newlines)

    def test_sync_off_on_this_mac_sends_nothing(self):
        # `hexabuddy.py sync off` (state.json sync:false): the hook stops before any network call; on again → sends.
        state = self.home / 'hb'; state.mkdir(); self.env['HEXABUDDY_HOME'] = str(state)
        (state / 'state.json').write_text(json.dumps({'sync': False}))
        for event in ('SessionStart', 'UserPromptSubmit', 'Stop'):
            r = self.hook('report-status.sh', {'session_id': 's1', 'hook_event_name': event, 'prompt': 'secret prompt'})
            self.assertEqual((r.returncode, r.stderr), (0, ''))
        time.sleep(1)
        self.assertEqual(Stub.posts, [], 'sync off: nothing leaves the Mac')
        (state / 'state.json').write_text(json.dumps({}))
        self.hook('report-status.sh', {'session_id': 's1', 'hook_event_name': 'UserPromptSubmit', 'prompt': 'hi'})
        self.wait_posts(1)
        self.assertEqual(len(Stub.posts), 1, 'sync on again: status is sent')

    def test_session_id_is_never_executed(self):
        r = self.hook('report-status.sh', {'session_id': f'$(touch {self.pwn})', 'hook_event_name': 'Stop'})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.wait_posts(1, 3); time.sleep(0.5)
        self.assertFalse(self.pwn.exists())

    def test_no_permission_hook_ships(self):
        # Permission prompts stay in the terminal (2026-11-17): no approve.sh, no PermissionRequest hook for either agent.
        self.assertFalse((HOOKS / 'approve.sh').exists())
        self.assertNotIn('PermissionRequest', json.loads((HOOKS / 'hooks.json').read_text())['hooks'])


if __name__ == '__main__':
    unittest.main()
