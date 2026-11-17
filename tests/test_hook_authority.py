"""S-05 on the Mac: text the plugin hooks carry (prompts, tool inputs, replies, server answers) is data, never an action.

report-status.sh and approve.sh run against a loopback stub server with the desktop key from the environment. Shell
syntax in a prompt, a tool command, a session id or a server reply must never run; the approval hook allows a tool
only on the exact decision "allow" for its own request, and anything else (unknown words, another request, a menu
answer carrying commands) never becomes an allow. No real server, Keychain or ~/.hexabuddy is touched.
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

    def test_session_id_is_never_executed(self):
        r = self.hook('report-status.sh', {'session_id': f'$(touch {self.pwn})', 'hook_event_name': 'Stop'})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.wait_posts(1, 3); time.sleep(0.5)
        self.assertFalse(self.pwn.exists())

    def approve(self, tool_input, decision=None):
        if decision is not None:
            Stub.decision = decision
        return self.hook('approve.sh', {'session_id': 's1', 'tool_name': 'Bash', 'tool_input': tool_input,
                                        'permission_suggestions': []})

    def emitted(self, r):
        return json.loads(r.stdout)['hookSpecificOutput']['decision'] if r.stdout.strip() else None

    def test_exact_allow_for_its_own_request_is_the_only_allow(self):
        r = self.approve({'command': 'ls'})
        self.assertEqual(self.emitted(r), {'behavior': 'allow'})                     # positive control
        for word in ['ALLOW', 'allow ', 'yes', 'allow_always', '', None, ['allow'], {'behavior': 'allow'}]:
            # ('allow\n' reads as allow: $(…) trims trailing newlines from the server's own decision; not a bypass)
            r = self.approve({'command': 'ls'}, {'decision': word})
            self.assertNotEqual((self.emitted(r) or {}).get('behavior'), 'allow', f'{word!r} became an allow')

    def test_tool_input_shell_text_is_forwarded_not_run(self):
        for text in self.evil:
            r = self.approve({'command': text, 'file_path': text}, {'decision': 'deny'})
            self.assertEqual(self.emitted(r), {'behavior': 'deny'})
        self.assertFalse(self.pwn.exists(), 'shell text in a tool input ran')
        sent = [json.loads(b)['tool_input']['command'] for p, b in Stub.posts if p.endswith('/approval-request')]
        self.assertEqual(sent, self.evil)

    def test_menu_answer_is_a_deny_message_never_an_action(self):
        for text in self.evil:
            r = self.approve({'command': 'ls'}, {'decision': 'answer', 'answer': text})
            d = self.emitted(r)
            self.assertEqual(d['behavior'], 'deny')
            self.assertTrue(d['message'].startswith('The user answered via Hexabuddy: '))
            self.assertIn(f'touch {self.pwn}', d['message'])                          # carried as text
        self.assertFalse(self.pwn.exists(), 'a phone answer ran as a command')
        self.assertFalse((self.project / '.claude/settings.local.json').exists(), 'an answer wrote an allow rule')

    def test_reply_text_from_the_server_is_never_run(self):
        r = self.approve({'command': 'ls'}, {'decision': f'$(touch {self.pwn})'})
        self.assertNotEqual((self.emitted(r) or {}).get('behavior'), 'allow')
        self.assertFalse(self.pwn.exists())


if __name__ == '__main__':
    unittest.main()
