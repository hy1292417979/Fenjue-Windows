import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fenjue.cockpit_guard import create_app, Ledger
from fenjue.outbound_control import configure, write_control
from tests.test_cockpit_guard import Source, output


class OutboundGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_credential_body_state_and_no_fallback(self):
        source = Source()
        source.settings = lambda: {'auth_file': 'fixture.json', 'default_effort': 'max'}
        requests = []
        def upstream(request):
            requests.append(request)
            return httpx.Response(200, json=output())
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(source, 'guard-key', Path(tmp), httpx.MockTransport(upstream))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1:17878') as client:
                payload = {'model': 'test-model', 'input': [{'type': 'compaction', 'encrypted_content': 'OPAQUE'}],
                           'reasoning': {'effort': 'high'}, 'instructions': 'KEEP'}
                headers = {'Authorization': 'Bearer guard-key', 'session_id': 'fixture', 'x-fenjue-oauth': 'SECRET_OAUTH',
                           'x-fenjue-account': 'account', 'x-fenjue-auth-id': 'fixture.json'}
                for override in ({'Authorization': 'Bearer COCKPIT_KEY'}, {'x-fenjue-oauth': 'other'}, {'x-fenjue-account': 'other'}, {'x-fenjue-auth-id': 'other'}):
                    r = await client.post('/cockpit/v1/responses', json=payload, headers=headers | override)
                    self.assertIn(r.status_code, (401, 409))
                self.assertEqual(requests, [])
                probe = await client.post('/cockpit/probe', json={}, headers=headers)
                self.assertEqual(probe.status_code, 200)
                self.assertFalse(probe.json()['generation_submitted'])
                self.assertEqual(requests, [])
                r = await client.post('/cockpit/v1/responses', json=payload, headers=headers)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0].headers['authorization'], 'Bearer SECRET_OAUTH')
                self.assertEqual(json.loads(requests[0].content)['input'], payload['input'])
                with Ledger(Path(tmp)).db() as db:
                    row = db.execute('SELECT route,status FROM calls').fetchone()
                self.assertEqual(row, ('cockpit_outbound', 'completed'))


class OutboundSwitchTests(unittest.TestCase):
    def test_disable_preserves_fields_and_waits_for_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'fenjue-guard').mkdir()
            control = directory / 'fenjue-guard/control.local.json'
            initial = {'schema': 1, 'enabled': True, 'guard_key': 'fixture', 'other': 'retained'}
            write_control(control, initial)
            with patch('fenjue.outbound_control.probe', side_effect=[{'enabled': False, 'active': 1}, {'enabled': False, 'active': 0}]), patch('fenjue.outbound_control.time.sleep'):
                self.assertTrue(configure('disable', directory, directory, source=object())['drained'])
            self.assertEqual(json.loads(control.read_text()), initial | {'enabled': False})

    def test_disable_failure_does_not_claim_drained(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'fenjue-guard').mkdir()
            write_control(directory / 'fenjue-guard/control.local.json', {'schema': 1, 'enabled': True})
            with patch('fenjue.outbound_control.probe', return_value={'enabled': False, 'active': 1}):
                with self.assertRaises(ValueError):
                    configure('disable', directory, directory, source=object(), drain_seconds=0)

    def test_enable_unknown_binary_never_writes_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'deployment.local.json').write_text(json.dumps({'patched_sha256': 'wrong'}))
            (directory / 'cockpit-cliproxy.exe').write_bytes(b'fixture')
            with self.assertRaises(ValueError):
                configure('enable', directory, directory, source=object())
            self.assertFalse((directory / 'fenjue-guard/control.local.json').exists())

if __name__ == '__main__':
    unittest.main()
