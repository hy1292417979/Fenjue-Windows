import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fenjue.guard_config import GUARD_URL, replace_url, configure

SAMPLE = '''model_provider = "codex_local_access"
model = "unchanged-model"
model_reasoning_effort = "medium"
[model_providers.codex_local_access]
base_url = "http://localhost:64492/v1" # retain this comment
wire_api = "responses"
experimental_bearer_token = "secret-test-fixture"
supports_websockets = false
[model_providers.other]
base_url = "https://example.invalid/v1"
[mcp_servers.fixture]
command = "preserve-me"
'''


class ConfigTests(unittest.TestCase):
    def test_only_target_url_changes_and_crlf_bom_preserved(self):
        raw = b'\xef\xbb\xbf' + SAMPLE.replace('\n', '\r\n').encode()
        changed = replace_url(raw, 'http://localhost:64492/v1', GUARD_URL)
        self.assertEqual(changed, raw.replace(b'http://localhost:64492/v1', GUARD_URL.encode()))

    def test_unexpected_provider_or_url_is_not_overwritten(self):
        with self.assertRaises(ValueError):
            replace_url(SAMPLE.replace('model_provider = "codex_local_access"', 'model_provider = "other"').encode(),
                        'http://localhost:64492/v1', GUARD_URL)
        with self.assertRaises(ValueError):
            replace_url(SAMPLE.encode(), 'different-url', GUARD_URL)

    def test_enable_requires_real_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'config.toml'
            config.write_text(SAMPLE)
            (root / 'acceptance.local.json').write_text(json.dumps({'passed': False}))
            with self.assertRaises(ValueError):
                configure('enable', config, root)
            self.assertEqual(config.read_text(), SAMPLE)

    def test_restore_does_not_overwrite_subsequent_user_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'config.toml'
            config.write_text(SAMPLE)
            self.assertFalse(configure('restore', config, root)['changed'])
            self.assertEqual(config.read_text(), SAMPLE)

    def test_other_active_provider_is_reported_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'config.toml'
            original = SAMPLE.replace('model_provider = "codex_local_access"', 'model_provider = "other"')
            config.write_text(original)
            self.assertFalse(configure('status', config, root)['codex_provider_is_cockpit'])
            self.assertFalse(configure('restore', config, root)['changed'])
            self.assertEqual(config.read_text(), original)

    def test_restore_changes_only_url_and_follows_current_cockpit_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'config.toml'
            original = SAMPLE.replace('http://localhost:64492/v1', GUARD_URL)
            config.write_text(original)
            (root / 'codex-url-rollback.local.json').write_text(json.dumps({
                'config_path': str(config.resolve()), 'provider': 'codex_local_access',
                'original_url': 'http://localhost:64492/v1'}))
            with patch('fenjue.cockpit.CockpitSource.connection', return_value=('http://127.0.0.1:65000', 'new-key')):
                self.assertTrue(configure('restore', config, root)['changed'])
            self.assertEqual(config.read_text(), original.replace(GUARD_URL, 'http://127.0.0.1:65000/v1'))


if __name__ == '__main__':
    unittest.main()
