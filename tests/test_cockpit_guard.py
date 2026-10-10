import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fenjue.cockpit_accounts import CockpitAccounts, Snapshot, BindingError, fingerprint
from fenjue.cockpit_guard import create_app, Ledger
from fenjue.guard_protocol import prepare, check_response, ProtocolError


def output(model='test-model', effort='high', items=None):
    return {'id': 'r1', 'object': 'response', 'status': 'completed', 'model': model,
            'reasoning': {'effort': effort}, 'output': items or [
                {'type': 'message', 'id': 'm1', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'ok'}]}]}


class Source:
    def __init__(self):
        self.value = Snapshot('account-fingerprint', 'controls', 'SECRET_OAUTH', 'account', None,
                              'http://127.0.0.1:64492', 'COCKPIT_KEY', False)
        self.dynamic = False
    def snapshot(self):
        return self.value
    def selected_snapshot(self, token, account_id, auth_id):
        if token == 'SECRET_OAUTH' and account_id == 'account' and auth_id == 'fixture.json':
            return self.value
        if token == 'OTHER_OAUTH' and account_id == 'other-account' and auth_id == 'other.json':
            return Snapshot('other-fingerprint', 'controls-other', token, account_id, None,
                            self.value.local_url, self.value.local_key, False)
        raise BindingError('selected Cockpit OAuth credential is unavailable or outside the API-key scope')
    def settings(self):
        return {'default_effort': 'max', 'timeout_seconds': 2, 'mode': 'dynamic' if self.dynamic else 'pinned'}
    def files(self):
        return ({}, {'modelRouting': {}}, {'accountIds': ['selected', 'other']}, [],
                self.value.local_url, self.value.local_key)
    def accepted_local_key(self):
        return 'COCKPIT_KEY'


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'host': '127.0.0.1', 'port': 64492, 'api-keys': ['local'], 'auth-dir': str(self.root)}))
        self.manifest = {'apiKeys': [{'key': 'local', 'enabled': True, 'accountIds': ['selected', 'other']}],
            'accounts': [{'id': 'selected', 'authKind': 'oauth', 'authId': 'auth.json', 'chatgptAccountId': 'account'},
                         {'id': 'other', 'authKind': 'api_key'}],
            'customRoutingRules': [{'accountId': 'selected', 'isPreferred': True}]}
        self.write_manifest()
        self.auth = {'access_token': 'TOKEN_1', 'account_id': 'account', 'expired': 9999999999}
        (self.root / 'auth.json').write_text(json.dumps(self.auth))
        self.source = CockpitAccounts(self.root / 'settings.json', self.config)

    def write_manifest(self):
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))

    def tearDown(self):
        self.tmp.cleanup()

    def test_preferred_oauth_is_pinned_without_copying_token(self):
        self.source.initialize()
        value = self.source.snapshot()
        self.assertEqual(value.account_ref, fingerprint('account'))
        self.assertFalse(value.cockpit_exclusive)
        self.assertNotIn('TOKEN_1', self.source.settings_path.read_text())
        self.assertNotIn('TOKEN_1', repr(value))

    def test_token_refresh_preserves_binding_but_identity_change_fails(self):
        self.source.initialize()
        original = self.source.snapshot().binding
        (self.root / 'auth.json').write_text(json.dumps(self.auth | {'access_token': 'TOKEN_2'}))
        self.assertEqual(self.source.snapshot().binding, original)
        (self.root / 'auth.json').write_text(json.dumps(self.auth | {'account_id': 'different'}))
        with self.assertRaises(BindingError):
            self.source.snapshot()

    def test_no_random_choice_and_removed_account_never_falls_back(self):
        self.source.initialize()
        self.manifest['accounts'] = self.manifest['accounts'][1:]
        self.write_manifest()
        with self.assertRaises(BindingError):
            self.source.snapshot()

    def test_single_account_scope_qualifies_cockpit(self):
        self.manifest['apiKeys'][0]['accountIds'] = ['selected']
        self.write_manifest()
        self.source.initialize()
        self.assertTrue(self.source.snapshot().cockpit_exclusive)

    def test_multi_account_scope_ignores_preferred_account_and_stays_dynamic(self):
        self.manifest['accounts'].append({
            'id': 'second', 'authKind': 'oauth', 'authId': 'second.json', 'chatgptAccountId': 'second-account'})
        self.manifest['apiKeys'][0]['accountIds'].append('second')
        self.manifest['customRoutingRules'].append({'accountId': 'second', 'isPreferred': True})
        self.write_manifest()
        self.assertEqual(self.source.initialize(), {'account_ref': 'dynamic', 'controls_version': fingerprint(self.source.settings())[:64]})
        self.assertEqual(self.source.settings()['mode'], 'dynamic')
        self.assertNotIn('account', self.source.settings())

    def test_selected_token_can_be_newer_than_credential_file(self):
        (self.root / 'auth.json').write_text(json.dumps(self.auth | {'expired': 0}))
        value = self.source.selected_snapshot('REFRESHED_TOKEN', 'account', 'auth.json')
        self.assertEqual(value.token, 'REFRESHED_TOKEN')
        self.assertEqual(value.account_ref, fingerprint('account'))

    def test_native_routing_metadata_is_not_a_model_alias(self):
        self.manifest['apiKeys'][0]['modelRouting'] = {'automatic': True, 'defaultRoute': 'oauth',
            'failurePolicy': 'strict', 'routes': [], 'nativeModels': ['test-model']}
        self.write_manifest()
        self.source.initialize()
        self.assertTrue(self.source.snapshot().token)

    def test_model_restrictions_are_never_bypassed(self):
        self.source.initialize()
        self.manifest['apiKeys'][0]['allowedModels'] = ['other-model']
        self.write_manifest()
        self.assertTrue(self.source.snapshot().token)
        with self.assertRaises(BindingError):
            self.source.validate_model('test-model')
        self.assertEqual(self.source.validate_model('other-model'), 'other-model')


class ProtocolTests(unittest.TestCase):
    def test_request_shape_rejects_invalid_model_instructions_and_effort(self):
        cases = (
            {'model': ' ', 'input': 'hello'},
            {'model': 'test-model', 'input': 'hello', 'instructions': 1},
            {'model': 'test-model', 'input': 'hello', 'reasoning': {'effort': 1}},
            {'model': 'test-model', 'input': 'hello', 'reasoning': None},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ProtocolError):
                    prepare(payload)

    def test_request_shape_keeps_modern_codex_fields_without_silent_rewrite(self):
        payload = {
            'model': 'test-model', 'input': [], 'instructions': 'KEEP',
            'reasoning': {'effort': 'high', 'summary': 'auto'},
            'tools': [{'type': 'custom', 'name': 'apply_patch'}],
            'metadata': {'client': 'codex'}, 'prompt_cache_key': 'session',
        }
        prepared = prepare(payload)
        for key in ('input', 'instructions', 'reasoning', 'tools', 'metadata', 'prompt_cache_key'):
            self.assertEqual(prepared[key], payload[key])

    def test_client_instructions_tools_and_explicit_effort_unchanged(self):
        p = {'model': 'test-model', 'instructions': 'EXACT', 'input': [{'type': 'reasoning', 'encrypted_content': 'OPAQUE'}],
             'tools': [{'type': 'custom', 'name': 'apply_patch'}], 'reasoning': {'effort': 'high'}, 'prompt_cache_key': 's'}
        prepared = prepare(p)
        for key in p:
            self.assertEqual(prepared[key], p[key])
        self.assertEqual(prepare({'model': 'test-model', 'input': 'hello'})['reasoning']['effort'], 'max')

    def test_mismatches_fail_and_missing_effort_is_unverified(self):
        p = prepare({'model': 'test-model', 'input': 'x', 'reasoning': {'effort': 'high'}})
        for r in (output(model='other'), output(effort='low'), output() | {'status': 'incomplete'}):
            with self.assertRaises(ProtocolError):
                check_response(r, p)
        r = output()
        r.pop('reasoning')
        self.assertFalse(check_response(r, p)['effort_verified'])

    def test_deliberate_effort_update_is_respected(self):
        p = prepare({'model': 'test-model', 'input': [{'type': 'configuration_update', 'reasoning': {'effort': 'low'}}], 'reasoning': {'effort': 'high'}})
        evidence = check_response(output(effort='high'), p)
        self.assertTrue(evidence['effort_verified'])
        self.assertEqual(evidence['effective_requested_effort'], 'low')
        self.assertFalse(evidence['effective_effort_verified'])
        with self.assertRaises(ProtocolError):
            check_response(output(effort='low'), p)

    def test_compaction_trigger_must_return_encrypted_state(self):
        p = prepare({'model': 'test-model', 'input': [{'type': 'compaction_trigger'}], 'reasoning': {'effort': 'high'}})
        with self.assertRaises(ProtocolError):
            check_response(output(), p)
        self.assertTrue(check_response(output(items=[{'type': 'compaction', 'encrypted_content': 'STATE'}]), p)['compaction_verified'])


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.source = Source()
        self.requests = []
        self.response = httpx.Response(200, json=output())
        def upstream(request):
            self.requests.append(request)
            return self.response
        self.app = create_app(self.source, 'GATEWAY_KEY', Path(self.tmp.name), httpx.MockTransport(upstream))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url='http://127.0.0.1:17878')
        self.auth = {'Authorization': 'Bearer COCKPIT_KEY', 'session_id': 'codex-session'}
        self.payload = {'model': 'test-model', 'input': 'hello', 'reasoning': {'effort': 'high'}}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tmp.cleanup()

    async def test_native_route_uses_pinned_token_not_pool_key(self):
        r = await self.client.post('/v1/responses', json=self.payload, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.requests[0].url.host, 'chatgpt.com')
        self.assertEqual(self.requests[0].headers['authorization'], 'Bearer SECRET_OAUTH')
        self.assertEqual(self.requests[0].headers['session_id'], 'codex-session')
        self.assertEqual(r.headers['x-fenjue-route'], 'codex_http')

    async def test_cockpit_selected_accounts_are_dynamic_but_session_sticky(self):
        payload = self.payload
        outbound_auth = {'Authorization': 'Bearer GATEWAY_KEY'}
        first = await self.client.post('/cockpit/v1/responses', json=payload,
                                       headers=outbound_auth | {'x-fenjue-oauth': 'SECRET_OAUTH',
                                                            'x-fenjue-account': 'account',
                                                            'x-fenjue-auth-id': 'fixture.json'})
        self.assertEqual(first.status_code, 200, first.text)
        second = await self.client.post('/cockpit/v1/responses', json=payload,
                                        headers=outbound_auth | {'session_id': 'other-session',
                                                             'x-fenjue-oauth': 'OTHER_OAUTH',
                                                             'x-fenjue-account': 'other-account',
                                                             'x-fenjue-auth-id': 'other.json'})
        self.assertEqual(second.status_code, 200, second.text)
        changed = await self.client.post('/cockpit/v1/responses', json=payload,
                                         headers=outbound_auth | {'session_id': 'other-session',
                                                              'x-fenjue-oauth': 'SECRET_OAUTH',
                                                              'x-fenjue-account': 'account',
                                                              'x-fenjue-auth-id': 'fixture.json'})
        self.assertEqual(changed.status_code, 409)

    async def test_dynamic_status_endpoint_reports_without_account_snapshot(self):
        self.source.dynamic = True
        response = await self.client.get('/api/status', headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['account_ref'], 'dynamic')
        self.assertFalse(response.json()['cockpit_same_account_comparison_available'])

    async def test_dynamic_catalog_uses_cockpit_scope_without_pinning_or_metadata(self):
        self.source.dynamic = True
        self.source.snapshot = lambda: (_ for _ in ()).throw(BindingError('No pinned account'))
        self.response = httpx.Response(200, json={'object': 'list', 'data': [
            {'id': 'test-model', 'account': 'PRIVATE'}, {'id': 'other-model'}]})
        response = await self.client.get('/v1/models', headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'object': 'list', 'data': [
            {'id': 'test-model', 'object': 'model'}, {'id': 'other-model', 'object': 'model'}]})
        self.assertEqual(self.requests[0].url.host, '127.0.0.1')
        self.assertEqual(self.requests[0].headers['authorization'], 'Bearer COCKPIT_KEY')
        with Ledger(Path(self.tmp.name)).db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM calls').fetchone()[0], 0)

    async def test_nonfinite_and_malformed_requests_never_reach_upstream(self):
        for content in (
                b'{"model":"test-model","input":"hello","temperature":1e999}',
                b'{"model":"test-model","model":"other","input":"hello"}',
                b'{"model":"test-model","input":"hello","instructions":12}',
                b'{"model":"test-model","input":"hello","store":0}'):
            response = await self.client.post('/v1/responses', content=content,
                                               headers=self.auth | {'Content-Type': 'application/json'})
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.requests, [])

    async def test_model_mismatch_never_reaches_codex_as_success(self):
        self.response = httpx.Response(200, json=output(model='other'))
        r = await self.client.post('/v1/responses', json=self.payload | {'stream': True}, headers=self.auth)
        self.assertTrue(r.status_code == 502 or 'fenjue_guard_rejected' in r.text)
        self.assertNotIn('response.completed', r.text)
        self.assertEqual(len(self.requests), 1)

    async def test_stream_preserves_upstream_turn_state_header(self):
        self.response = httpx.Response(200, json=output(), headers={'x-codex-turn-state': 'opaque-header'})
        r = await self.client.post('/v1/responses', json=self.payload | {'stream': True}, headers=self.auth)
        self.assertEqual(r.headers['x-codex-turn-state'], 'opaque-header')
        self.assertIn('response.completed', r.text)

    async def test_modern_custom_tool_and_opaque_items_reconciled(self):
        items = [{'type': 'reasoning', 'id': 'rs', 'encrypted_content': 'OPAQUE', 'summary': []},
                 {'type': 'custom_tool_call', 'id': 'tc', 'call_id': 'c1', 'name': 'apply_patch', 'input': 'PATCH'}]
        events = [{'type': 'response.output_item.done', 'output_index': i, 'item': item} for i, item in enumerate(items)]
        events.append({'type': 'response.completed', 'response': output(items=items) | {'output': []}})
        wire = ''.join('data: ' + json.dumps(e) + '\n\n' for e in events)
        self.response = httpx.Response(200, content=wire, headers={'Content-Type': 'text/event-stream'})
        r = await self.client.post('/v1/responses', json=self.payload | {'stream': True}, headers=self.auth)
        self.assertIn('custom_tool_call', r.text)
        self.assertIn('OPAQUE', r.text)
        self.assertIn('response.completed', r.text)
        with Ledger(Path(self.tmp.name)).db() as db:
            rows = str(db.execute('SELECT * FROM calls').fetchall())
        self.assertNotIn('OPAQUE', rows)
        self.assertNotIn('PATCH', rows)
        self.assertNotIn('SECRET_OAUTH', rows)

    async def test_standard_codex_session_binding_no_custom_echo_needed(self):
        first = await self.client.post('/v1/responses', json=self.payload, headers=self.auth)
        self.assertEqual(first.status_code, 200)
        self.source.value.account_ref = 'different'
        blocked = await self.client.post('/v1/responses', json=self.payload, headers=self.auth)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(len(self.requests), 1)

    async def test_mixed_pool_cannot_be_used_for_same_account_check(self):
        r = await self.client.post('/api/check', json={'model': 'test-model', 'approve_calls': 2}, headers=self.auth)
        self.assertEqual(r.status_code, 409)
        r = await self.client.post('/v1/responses', json=self.payload, headers=self.auth | {'x-fenjue-route': 'cockpit'})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.requests, [])

    async def test_compact_translates_to_native_trigger_once_preserving_history(self):
        items = [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'retained'}]},
                 {'type': 'compaction', 'encrypted_content': 'OPAQUE_COMPACT'}]
        result = output(effort='max', items=items) | {'created_at': 123, 'usage': {'input_tokens': 20}}
        self.response = httpx.Response(200, json=result)
        payload = {'model': 'test-model', 'instructions': 'PRESERVE', 'input': [{'role': 'user', 'content': 'hello'}]}
        r = await self.client.post('/v1/responses/compact', json=payload, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['output'], items)
        self.assertEqual(r.json()['object'], 'response.compaction')
        actual = json.loads(self.requests[0].content)
        self.assertEqual(actual['input'], payload['input'] + [{'type': 'compaction_trigger'}])
        self.assertEqual(actual['instructions'], payload['instructions'])
        self.assertTrue(str(self.requests[0].url).endswith('/responses'))
        self.assertEqual(len(self.requests), 1)

    async def test_direct_v2_compaction_stream_preserves_opaque_state(self):
        item = {'type': 'compaction', 'id': 'cmp', 'encrypted_content': 'OPAQUE'}
        events = [{'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                  {'type': 'response.completed', 'response': output(items=[item]) | {'output': []}}]
        self.response = httpx.Response(200, content=''.join('data: ' + json.dumps(e) + '\n\n' for e in events),
                                       headers={'Content-Type': 'text/event-stream'})
        r = await self.client.post('/v1/responses', json=self.payload | {'input': [{'type': 'compaction_trigger'}], 'stream': True}, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('OPAQUE', r.text)
        self.assertIn('response.completed', r.text)

    async def test_auth_and_redirect_protection(self):
        denied = await self.client.post('/v1/responses', json=self.payload)
        self.assertEqual(denied.status_code, 401)
        self.response = httpx.Response(302, headers={'Location': 'https://evil.test/'})
        r = await self.client.post('/v1/responses', json=self.payload, headers=self.auth)
        self.assertEqual(r.status_code, 502)
        self.assertEqual(len(self.requests), 1)


if __name__ == '__main__':
    unittest.main()
