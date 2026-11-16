"""translate-to-buddy.py against a stub Hexabuddy server on loopback: card and sentence paths, 423, 429, failures."""
import importlib.util
import io
import json
import pathlib
import ssl
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('translate', pathlib.Path(__file__).parents[1] / 'hexabuddy-workspace/translate-to-buddy/translate-to-buddy.py')
translate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(translate)

KEY = 'hb_desktop_' + 'a' * 64
CARD = {'zh': '伙伴', 'en': 'companion', 'ipa': '/kəmˈpænjən/'}


class StubServer:
    """Records every request; replies[path] is (status, body) or a list of them (one per call)."""

    def __init__(self):
        self.calls = []
        self.replies = {'/translate/card': (200, CARD), '/translate/text': (200, {'text': '早上好'}),
                        '/card': (200, {'ok': True, 'shown': True}), '/say': (200, {'ok': True, 'spoke': True})}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                stub.calls.append((self.path, body, self.headers.get('Authorization')))
                reply = stub.replies[self.path]
                status, payload = reply.pop(0) if isinstance(reply, list) else reply
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.http.server_address[1]}'

    def paths(self):
        return [path for path, _, _ in self.calls]


class TranslateTests(unittest.TestCase):
    def setUp(self):
        self.server = StubServer()
        self.addCleanup(self.server.http.server_close)
        self.addCleanup(self.server.http.shutdown)

    def run_main(self, text):
        env = {'HEXABUDDY_SERVER': self.server.url, 'HEXABUDDY_CA_CERT': '', 'HEXABUDDY_API_KEY': KEY}
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(translate.os.environ, env), patch.object(translate.sys, 'argv', ['translate', text]), \
             patch.object(translate, '_load_server_env'), patch.object(translate.time, 'sleep'), \
             redirect_stdout(out), redirect_stderr(err):
            try:
                translate.main()
                code = 0
            except RuntimeError as error:
                code, message = 1, str(error)
                err.write(message)
        return code, out.getvalue(), err.getvalue()

    def test_word_is_translated_on_the_server_and_carded_without_a_mask(self):
        code, out, _ = self.run_main('companion')
        self.assertEqual(code, 0)
        self.assertEqual(self.server.paths(), ['/translate/card', '/card'])
        self.assertEqual(self.server.calls[0][1], {'text': 'companion'})
        self.assertEqual(self.server.calls[1][1], {**CARD, 'say': 'companion，伙伴'})
        self.assertTrue(all(auth == 'Bearer ' + KEY for _, _, auth in self.server.calls))
        self.assertEqual(out.strip(), '伙伴 / companion /kəmˈpænjən/')

    def test_sentence_is_translated_by_language_then_spoken(self):
        code, out, _ = self.run_main('Good morning to everyone in the room')
        self.assertEqual(code, 0)
        self.assertEqual(self.server.paths(), ['/translate/text', '/say'])
        self.assertEqual(self.server.calls[0][1], {'text': 'Good morning to everyone in the room', 'source': 'en-US', 'target': 'zh-CN'})
        self.assertEqual(self.server.calls[1][1], {'text': '早上好', 'ttl': 10})
        self.server.calls.clear()
        self.run_main('今天天气很好，我们一起去公园散步吧')
        self.assertEqual(self.server.calls[0][1]['source'], 'zh-CN')
        self.assertEqual(self.server.calls[0][1]['target'], 'en-US')

    def test_vocabulary_switched_off_speaks_without_a_card(self):
        self.server.replies['/card'] = (423, {'error': 'vocabulary is switched off for this Mac', 'disabled': 'vocabulary'})
        code, _, _ = self.run_main('companion')
        self.assertEqual(code, 0)
        self.assertEqual(self.server.paths(), ['/translate/card', '/card', '/say'])
        self.assertEqual(self.server.calls[2][1], {'text': 'companion，伙伴', 'ttl': 60})

    def test_quota_reached_is_one_clear_line_and_nothing_is_sent(self):
        for path, text in (('/translate/card', 'companion'), ('/translate/text', 'Good morning to everyone in the room')):
            self.server.calls.clear()
            self.server.replies[path] = (429, {'error': 'daily translation limit reached', 'code': 'quota'})
            code, _, err = self.run_main(text)
            self.assertEqual(code, 1)
            self.assertEqual(self.server.paths(), [path])  # an HTTP answer is never retried or charged twice
            self.assertEqual(err.strip().splitlines(), ['the daily translation limit for this account is reached; try again tomorrow.'])

    def test_failed_card_translation_falls_back_to_speaking_a_translation(self):
        self.server.replies['/translate/card'] = (502, {'error': 'Translation provider returned no card'})
        code, out, _ = self.run_main('companion')
        self.assertEqual(code, 0)
        self.assertEqual(self.server.paths(), ['/translate/card', '/translate/text', '/say'])
        self.assertEqual(out.strip(), '早上好')

    def test_failed_save_is_not_reported_as_successful_translation(self):
        self.server.replies['/card'] = (500, {'error': 'boom'})
        code, _, err = self.run_main('companion')
        self.assertEqual(code, 1)
        self.assertIn('Could not save vocabulary', err)
        self.assertNotIn('/say', self.server.paths())

    def test_empty_translation_is_never_spoken(self):
        self.server.replies['/translate/text'] = (200, {'text': '  '})
        code, _, err = self.run_main('Good morning to everyone in the room')
        self.assertEqual(code, 1)
        self.assertIn('no translation', err)
        self.assertEqual(self.server.paths(), ['/translate/text'])

    def test_network_blip_is_retried_once_for_say(self):
        calls = []
        real = translate.server_opener

        def opener():
            inner = real()

            class Flaky:
                def open(self, req, timeout):
                    calls.append(req.full_url)
                    if len(calls) == 1:
                        raise translate.urllib.error.URLError('reset')
                    return inner.open(req, timeout=timeout)
            return Flaky()
        with patch.object(translate, 'server_opener', opener):
            code, _, _ = self.run_main('Good morning to everyone in the room')
        self.assertEqual(code, 1)  # /translate/text is not retried (tries=1)
        calls.clear()
        with patch.dict(translate.os.environ, {'HEXABUDDY_SERVER': self.server.url, 'HEXABUDDY_API_KEY': KEY}), \
             patch.object(translate, 'server_opener', opener), patch.object(translate.time, 'sleep'):
            translate.say('hi')
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.server.paths()[-1], '/say')

    def test_no_local_model_or_image_library(self):
        source = pathlib.Path(translate.__file__).read_text()
        for name in ('siliclub', 'PIL', 'load_llms', 'call_llm', 'render_mask', 'card-cache', 'mask'):
            self.assertNotIn(name, source.replace('No mask', '').replace('no mask', ''))

    def test_default_tls_verifies_certificate_and_hostname(self):
        with patch.dict(translate.os.environ, {'HEXABUDDY_CA_CERT': ''}):
            context = translate.server_ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_configured_certificate_used_for_server(self):
        with patch.dict(translate.os.environ, {'HEXABUDDY_CA_CERT': '/trusted/server.pem'}), patch.object(translate.ssl, 'create_default_context') as create:
            translate.server_ssl_context()
        create.assert_called_once_with(cafile='/trusted/server.pem')

    def test_desktop_key_is_the_only_credential(self):
        with patch.dict(translate.os.environ, {'HEXABUDDY_SERVER': 'https://example.test', 'HEXABUDDY_API_KEY': KEY}):
            headers = translate.server_headers()
        self.assertEqual(headers['Authorization'], 'Bearer ' + KEY)
        self.assertNotIn('x-hexabuddy-token', headers)

    def test_not_connected_fails_with_a_clear_message(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(translate.os.environ, {'HEXABUDDY_HOME': home}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'not connected'):
                translate._load_server_env()


if __name__ == '__main__':
    unittest.main()
