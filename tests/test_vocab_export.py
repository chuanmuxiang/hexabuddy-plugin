"""hexabuddy-workspace/scripts/vocab-export.py against a loopback stub server: formats, filters, audio, auth and HTTPS rule.

A-10 Vocabulary on the Mac: the words Translate → Buddy saved come back with the desktop key only, as a table, JSON
or flashcard CSV; per-word audio is saved without leaving the output folder; a rejected key or a plain-HTTP remote
server exits with a clear message and writes nothing. No real server, Keychain or ~/.hexabuddy is touched.
"""
import csv
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCRIPT = Path(__file__).parents[1] / 'hexabuddy-workspace/scripts/vocab-export.py'
KEY = 'hb_desktop_' + 'a' * 64
NOW = int(time.time() * 1000)
ITEMS = [
    {'i': 0, 'en': 'hedge', 'zh': '树篱', 'ipa': 'hedʒ', 'ts': NOW - 3600_000},
    {'i': 1, 'en': '../../escape', 'zh': '逃逸', 'ipa': '', 'ts': NOW - 2 * 86400_000},
    {'i': 2, 'en': '', 'zh': '', 'ipa': '', 'ts': NOW - 3 * 86400_000},          # bitmap-only (pre-text entry)
    {'i': 3, 'en': 'old', 'zh': '旧', 'ipa': 'oʊld', 'ts': NOW - 40 * 86400_000},
]


class Stub(BaseHTTPRequestHandler):
    seen = []
    status = 200

    def log_message(self, *a):
        pass

    def do_GET(self):
        Stub.seen.append((self.path, self.headers.get('Authorization')))
        if self.headers.get('Authorization') != 'Bearer ' + KEY or Stub.status != 200:
            self.send_response(401 if Stub.status == 200 else Stub.status); self.end_headers(); return
        url = urlparse(self.path)
        if url.path == '/vocab/list':
            body = json.dumps({'items': ITEMS}).encode()
        elif url.path == '/vocab/audio':
            body = b'RIFF' + parse_qs(url.query)['w'][0].encode()
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)


class VocabExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Stub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = 'http://127.0.0.1:%d' % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Stub.seen.clear(); Stub.status = 200
        self.dir = tempfile.TemporaryDirectory(); self.home = Path(self.dir.name)
        sentinel = self.home / '.hexabuddy/settings.json'; sentinel.parent.mkdir()
        sentinel.write_text(json.dumps({'server': 'https://wrong.example', 'token': 'secret'}))
        self.env = {'HOME': str(self.home), 'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                    'HEXABUDDY_SERVER': self.url, 'HEXABUDDY_API_KEY': KEY}

    def tearDown(self):
        self.dir.cleanup()

    def run_export(self, *args, **env):
        return subprocess.run(['python3', str(SCRIPT), *args], text=True, capture_output=True,
                              env=dict(self.env, **env), cwd=self.home, timeout=60)

    def test_table_lists_every_word_newest_first_with_the_desktop_key(self):
        r = self.run_export()
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertTrue(lines[0].split()[:2] == ['#', 'added'])
        self.assertIn('hedge', lines[2]); self.assertIn('(bitmap only)', r.stdout)
        self.assertEqual(Stub.seen, [('/vocab/list', 'Bearer ' + KEY)])     # one request, the key only as Bearer

    def test_json_backup_keeps_every_field(self):
        out = self.home / 'vocab.json'
        r = self.run_export('--json', '-o', str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(out.read_text())
        self.assertEqual([d['i'] for d in data], [0, 1, 2, 3])
        self.assertEqual(data[0]['zh'], '树篱'); self.assertTrue(data[0]['added'])
        self.assertIn('4 of 4 words', r.stderr)

    def test_csv_for_flashcards_skips_bitmap_only_entries(self):
        r = self.run_export('--csv')
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = list(csv.reader(io.StringIO(r.stdout)))
        self.assertEqual(rows[0], ['en', 'zh', 'ipa', 'added', 'ts'])
        self.assertEqual([row[0] for row in rows[1:]], ['hedge', '../../escape', 'old'])
        self.assertIn('skipped 1 bitmap-only', r.stderr)

    def test_filters_since_grep_limit(self):
        self.assertEqual([d['en'] for d in json.loads(self.run_export('--json', '--since', '7d').stdout)], ['hedge', '../../escape', ''])
        self.assertEqual([d['en'] for d in json.loads(self.run_export('--json', '--grep', '树').stdout)], ['hedge'])
        self.assertEqual(len(json.loads(self.run_export('--json', '--limit', '1').stdout)), 1)
        bad = self.run_export('--since', 'yesterday')
        self.assertEqual(bad.returncode, 1); self.assertIn('--since wants', bad.stderr)

    def test_audio_stays_inside_the_output_folder(self):
        folder = self.home / 'pronounce'
        r = self.run_export('--json', '--audio', str(folder))
        self.assertEqual(r.returncode, 0, r.stderr)
        names = sorted(p.name for p in folder.iterdir())
        self.assertEqual(names, ['.._.._escape.wav', 'hedge.wav', 'old.wav'])   # slashes replaced: no path escape
        self.assertFalse((self.home / 'escape.wav').exists())
        self.assertIn('3 saved', r.stderr)

    def test_rejected_key_is_a_clear_error_and_writes_nothing(self):
        out = self.home / 'v.json'
        r = self.run_export('--json', '-o', str(out), HEXABUDDY_API_KEY='hb_desktop_' + 'b' * 64)
        self.assertEqual(r.returncode, 1)
        self.assertIn('rejected the desktop key (HTTP 401)', r.stderr)
        self.assertFalse(out.exists())

    def test_plain_http_remote_server_is_refused_before_any_request(self):
        r = self.run_export(HEXABUDDY_SERVER='http://remote.example')
        self.assertEqual(r.returncode, 1)
        self.assertIn('HTTPS', r.stderr)
        self.assertEqual(Stub.seen, [])

    def test_not_connected_says_so(self):
        env = dict(self.env); env.pop('HEXABUDDY_API_KEY'); env.pop('HEXABUDDY_SERVER')
        r = subprocess.run(['python3', str(SCRIPT)], text=True, capture_output=True, env=env, cwd=self.home, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn('not connected', r.stderr)


if __name__ == '__main__':
    unittest.main()
