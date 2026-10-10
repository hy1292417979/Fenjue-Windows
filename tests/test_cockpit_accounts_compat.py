import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from fenjue.cockpit import CockpitSource
from fenjue.cockpit_accounts import BindingError, CockpitAccounts, Snapshot


class CockpitCompatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / 'config.json'
        self.config = {
            'host': '127.0.0.1', 'port': 61000, 'api-keys': ['fixture-key'],
            'auth-dir': str(self.root),
        }
        self.manifest = {
            'apiKeys': [{'id': 'fixture-id', 'key': 'fixture-key', 'enabled': True,
                         'accountIds': ['one']}],
            'accounts': [{'id': 'one', 'authKind': 'oauth', 'authId': 'one.json',
                          'chatgptAccountId': 'account-one'}],
            'modelIds': ['gpt-6-astra'],
        }
        self.write_files()
        (self.root / 'one.json').write_text(json.dumps({'account_id': 'account-one', 'access_token': 'fixture-token'}))

    def tearDown(self):
        self.tmp.cleanup()

    def write_files(self):
        self.config_path.write_text(json.dumps(self.config))
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))

    def source(self):
        return CockpitAccounts(self.root / 'settings.json', self.config_path)

    def test_connection_from_config_and_files_share_one_config_read(self):
        source = self.source()
        with patch.dict('os.environ', {}, clear=True), patch.object(CockpitSource, 'connection', side_effect=AssertionError('reread')):
            config, _, _, _, url, key = source.files()
        self.assertEqual(config, self.config)
        self.assertEqual((url, key), ('http://127.0.0.1:61000', 'fixture-key'))

    def test_native_codex_headers_are_forwarded_once(self):
        snapshot = Snapshot('account', 'controls', 'fixture-token', 'account-one', None,
                            'http://127.0.0.1:61000', 'fixture-key', True)
        incoming = {
            'openai-beta': 'caller-value', 'session_id': 'session',
            'version': '0.160.1', 'x-codex-turn-metadata': 'metadata',
            'x-codex-window-id': 'window', 'thread-id': 'thread',
            'x-openai-internal-codex-responses-lite': 'true',
        }
        request = httpx.Request('POST', 'https://example.invalid', headers=snapshot.headers('codex_http', incoming))
        self.assertEqual(request.headers.get_list('openai-beta'), ['caller-value'])
        for name, value in {
            'session_id': 'session', 'version': '0.160.1',
            'x-codex-turn-metadata': 'metadata', 'x-codex-window-id': 'window',
            'thread-id': 'thread', 'x-openai-internal-codex-responses-lite': 'true',
        }.items():
            self.assertEqual(request.headers.get(name), value)

        hyphenated = snapshot.headers('codex_http', {'Session-Id': 'session'})
        self.assertEqual(hyphenated.get('Session_id'), 'session')
        with self.assertRaises(BindingError):
            snapshot.headers('codex_http', {'session_id': 'one', 'Session-Id': 'two'})

    def test_dynamic_and_pinned_proxy_schemes_are_checked(self):
        self.config['proxy-url'] = 'ftp://127.0.0.1:21'
        self.write_files()
        source = self.source()
        with self.assertRaises(BindingError):
            source.selected_snapshot('fixture-token', 'account-one', 'one.json')
        with self.assertRaises(BindingError):
            source.initialize()
        self.manifest['accounts'].append({'id': 'two', 'authKind': 'oauth', 'authId': 'two.json',
                                          'chatgptAccountId': 'account-two'})
        self.manifest['apiKeys'][0]['accountIds'] = ['one', 'two']
        self.write_files()
        source.initialize()
        with self.assertRaises(BindingError):
            source.selected_snapshot('fixture-token', 'account-one', 'one.json')

    def test_model_policy_is_explicit_and_wildcards_are_case_insensitive(self):
        self.manifest['apiKeys'][0]['allowedModels'] = ['gpt-*']
        self.manifest['apiKeys'][0]['excludedModels'] = ['gpt-bad*']
        self.write_files()
        source = self.source()
        self.assertEqual(source.validate_model('GPT-6-ASTRA'), 'GPT-6-ASTRA')
        with self.assertRaises(BindingError):
            source.validate_model('gpt-bad-tool')
        with self.assertRaises(BindingError):
            source.validate_model('claude-3')

    def test_scope_order_does_not_change_binding_but_auth_entry_does(self):
        source = self.source()
        self.manifest['apiKeys'][0]['accountIds'] = ['one', 'two']
        self.manifest['accounts'].append({'id': 'two', 'authKind': 'oauth', 'authId': 'two.json',
                                          'chatgptAccountId': 'account-two'})
        (self.root / 'two.json').write_text(json.dumps({'account_id': 'account-two', 'access_token': 'other-token'}))
        self.write_files()
        other = source.selected_snapshot('other-token', 'account-two', 'two.json').binding
        first = source.selected_snapshot('fixture-token', 'account-one', 'one.json').binding
        self.manifest['apiKeys'][0]['accountIds'] = ['two', 'one']
        self.write_files()
        first_again = source.selected_snapshot('fixture-token', 'account-one', 'one.json').binding
        self.assertEqual(first_again, first)
        self.assertNotEqual(other, first)

    def test_zero_oauth_scope_fails_and_single_to_multi_migrates(self):
        self.manifest['accounts'] = [{'id': 'api', 'authKind': 'api_key', 'authId': 'api.json'}]
        self.write_files()
        with self.assertRaises(BindingError):
            self.source().initialize()

        self.manifest['accounts'] = [{'id': 'one', 'authKind': 'oauth', 'authId': 'one.json',
                                      'chatgptAccountId': 'account-one'}]
        self.manifest['apiKeys'][0]['accountIds'] = ['one']
        self.write_files()
        source = self.source()
        source.initialize()
        self.manifest['accounts'].append({'id': 'two', 'authKind': 'oauth', 'authId': 'two.json',
                                          'chatgptAccountId': 'account-two'})
        self.manifest['apiKeys'][0]['accountIds'] = ['one', 'two']
        self.write_files()
        self.assertEqual(source.initialize()['account_ref'], 'dynamic')
        self.assertEqual(source.settings()['mode'], 'dynamic')


if __name__ == '__main__':
    unittest.main()
