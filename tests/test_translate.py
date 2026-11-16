import importlib.util
import pathlib
import ssl
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('translate', pathlib.Path(__file__).parents[1] / 'hexabuddy-workspace/translate-to-buddy/translate-to-buddy.py')
translate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(translate)

class TranslateTests(unittest.TestCase):
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
        key = 'hb_desktop_' + 'a' * 64
        with patch.dict(translate.os.environ, {'HEXABUDDY_SERVER': 'https://example.test', 'HEXABUDDY_API_KEY': key}):
            headers = translate.server_headers()
        self.assertEqual(headers['Authorization'], 'Bearer ' + key)
        self.assertNotIn('x-hexabuddy-token', headers)

    def test_not_connected_fails_with_a_clear_message(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(translate.os.environ, {'HEXABUDDY_HOME': home}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'not connected'):
                translate._load_server_env()

    def test_missing_llm_settings_fail_with_a_clear_message(self):
        with patch.object(translate, 'SILICLUB', '/nonexistent/settings.json'):
            with self.assertRaisesRegex(RuntimeError, 'needs an LLM configured in ~/.siliclub/settings.json'):
                translate.load_llms()

    def test_vocabulary_switched_off_speaks_without_a_card(self):
        import io, urllib.error
        off = urllib.error.HTTPError('https://example.test/card', 423, 'vocabulary off', {}, io.BytesIO(b'{}'))
        with patch.object(translate.sys, 'argv', ['translate', 'companion']), patch.object(translate, '_load_server_env'), patch.object(translate, 'load_llms', return_value=[]), patch.object(translate, 'card_fields', return_value={'en':'companion','zh':'伙伴','ipa':''}), patch.object(translate, 'card', side_effect=off), patch.object(translate, 'say') as say, patch.object(translate, '_log_history'):
            translate.main()
        say.assert_called_once_with('companion，伙伴', ttl=60)

    def test_failed_save_is_not_reported_as_successful_translation(self):
        with patch.object(translate.sys, 'argv', ['translate', 'companion']), patch.object(translate, '_load_server_env'), patch.object(translate, 'load_llms', return_value=[]), patch.object(translate, 'card_fields', return_value={'en':'companion','zh':'伙伴','ipa':''}), patch.object(translate, 'card', side_effect=OSError('unreachable')), patch.object(translate, 'say') as say, patch.object(translate, '_log_history') as history:
            with self.assertRaisesRegex(RuntimeError, 'Could not save vocabulary'):
                translate.main()
            say.assert_not_called()
            history.assert_not_called()

if __name__ == '__main__':
    unittest.main()
