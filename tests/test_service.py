"""Catch lost Responses state, unbounded retries, stale routing and exposed access."""
import copy
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

import httpx

from fenjue.discovery.transport import completed


class EnvelopeTests(unittest.TestCase):
    def test_full_response_survives_but_is_not_in_research_log(self):
        body = {'id': 'resp_actual', 'object': 'response', 'status': 'completed',
                'model': 'gpt-6-astra', 'reasoning': {'effort': 'high'},
                'output': [{'type': 'reasoning', 'encrypted_content': 'OPAQUE'},
                           {'type': 'function_call', 'call_id': 'call_real', 'name': 'lookup', 'arguments': '{}'}],
                'usage': {'output_tokens': 17, 'output_tokens_details': {'reasoning_tokens': 11}}}
        result = completed(body, {'model': 'gpt-6-astra', 'reasoning': {'effort': 'high'}})
        self.assertEqual(getattr(result, 'response', None), body)
        self.assertNotIn('OPAQUE', json.dumps(result.record()))


class FixtureBackend:
    """External inference boundary; never contacts a real account."""
    def __init__(self):
        self.calls = []
        self.fail = False
        self.identity = 'account-fixture'

    async def inventory(self):
        return {'account_ref': self.identity, 'controls_version': 'v1', 'account_id': 1, 'group_id': 8}

    async def execute(self, route, payload):
        self.calls.append((route, copy.deepcopy(payload)))
        if self.fail:
            raise RuntimeError('network_error')
        return {'id': 'resp_real', 'object': 'response', 'status': 'completed',
                'model': payload['model'], 'reasoning': payload['reasoning'],
                'output': [{'type': 'reasoning', 'id': 'rs1', 'encrypted_content': 'OPAQUE'},
                           {'type': 'function_call', 'id': 'fc1', 'call_id': 'call_1',
                            'name': 'lookup', 'arguments': '{}'}],
                'usage': {'output_tokens': 17, 'output_tokens_details': {'reasoning_tokens': 11}}}, {'account_ref': self.identity, 'controls_version': 'v1'}


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from fenjue.service import create_app
        self.tmp = tempfile.TemporaryDirectory()
        self.backend = FixtureBackend()
        self.app = create_app(self.backend, Path(self.tmp.name), 'test-secret')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://127.0.0.1:17877')
        self.auth = {'Authorization': 'Bearer test-secret'}
        self.payload = {'model': 'gpt-6-astra', 'input': 'hello', 'reasoning': {'effort': 'high'}}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tmp.cleanup()

    async def test_auth_origin_host_gate_before_inference(self):
        for headers in ({}, self.auth | {'Origin': 'https://hostile.test'}, self.auth | {'Host': 'hostile.test'}):
            r = await self.client.post('/v1/responses', json=self.payload, headers=headers)
            self.assertIn(r.status_code, (401, 403))
        self.assertEqual(self.backend.calls, [])
        page = await self.client.get('/')
        self.assertNotIn('test-secret', page.text)

    async def test_full_envelope_and_tools_and_effort_are_preserved(self):
        p = self.payload | {'instructions': 'exact instruction', 'reasoning': {'effort': 'xhigh'},
                            'tools': [{'type': 'function', 'name': 'lookup', 'parameters': {'type': 'object'}}]}
        r = await self.client.post('/v1/responses', json=p, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['id'], 'resp_real')
        self.assertEqual(r.json()['output'][0]['encrypted_content'], 'OPAQUE')
        self.assertEqual(r.json()['usage']['output_tokens_details']['reasoning_tokens'], 11)
        sent = self.backend.calls[0][1]
        self.assertEqual(sent['tools'], p['tools'])
        self.assertEqual(sent['instructions'], 'exact instruction')
        self.assertEqual(sent['reasoning']['effort'], 'xhigh')
        self.assertFalse(sent['store'])
        self.assertEqual(sent['input'][0]['content'], 'hello')
        self.assertEqual(r.headers['x-fenjue-route'], 'codex_http')
        self.assertIn('x-fenjue-session', r.headers)

    async def test_opaque_continuation_requires_matching_signed_route(self):
        first = await self.client.post('/v1/responses', json=self.payload, headers=self.auth)
        p = self.payload | {'input': first.json()['output'] + [{'type': 'function_call_output', 'call_id': 'call_1', 'output': 'ok'}]}
        token = first.headers['x-fenjue-session']
        denied = await self.client.post('/v1/responses', json=p, headers=self.auth)
        self.assertEqual(denied.status_code, 409)
        denied = await self.client.post('/v1/responses', json=p, headers=self.auth | {'X-Fenjue-Session': token, 'X-Fenjue-Route': 'sub2api'})
        self.assertEqual(denied.status_code, 409)
        good = await self.client.post('/v1/responses', json=p, headers=self.auth | {'X-Fenjue-Session': token})
        self.assertEqual(good.status_code, 200)
        self.assertEqual(self.backend.calls[-1][1]['input'], p['input'])
        self.backend.identity = 'different-account'
        denied = await self.client.post('/v1/responses', json=p, headers=self.auth | {'X-Fenjue-Session': token})
        self.assertEqual(denied.status_code, 409)

    async def test_failure_never_replays_or_changes_route(self):
        self.backend.fail = True
        r = await self.client.post('/v1/responses', json=self.payload, headers=self.auth)
        self.assertEqual(r.status_code, 502)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertNotIn('test-secret', r.text)

    async def test_unknown_params_and_server_state_rejected_explicitly(self):
        for extra in ({'temperature': 0.4}, {'previous_response_id': 'resp_x'}, {'max_output_tokens': 32}, {'store': True}):
            r = await self.client.post('/v1/responses', json=self.payload | extra, headers=self.auth)
            self.assertEqual(r.status_code, 400)
        self.assertEqual(self.backend.calls, [])

    async def test_buffered_stream_preserves_final_response(self):
        r = await self.client.post('/v1/responses', json=self.payload | {'stream': True}, headers=self.auth)
        self.assertEqual(r.status_code, 200)
        events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith('data: ')]
        self.assertEqual(events[-1]['type'], 'response.completed')
        self.assertEqual(events[-1]['response']['id'], 'resp_real')
        self.assertEqual(events[-1]['response']['output'][0]['encrypted_content'], 'OPAQUE')
        self.assertEqual(r.headers['x-fenjue-delivery'], 'buffered')

    async def test_duplicate_json_and_oversized_input_never_reach_upstream(self):
        for raw, code in ((b'{"input":"first","input":"second"}', 400),
                          (b'{"input":NaN}', 400), (b'x' * 2_000_001, 413)):
            r = await self.client.post('/v1/responses', content=raw, headers=self.auth)
            self.assertEqual(r.status_code, code)
        self.assertEqual(self.backend.calls, [])

    async def test_checks_grade_answers_choose_route_and_enforce_two_call_budget(self):
        # Known external answers: 223*223+223 = 49952. Wrong direct answer must
        # switch the next auto call to the correctly answering Sub2API route.
        original = self.backend.execute
        async def answer(route, payload):
            result, info = await original(route, payload)
            answer = {'total': 49952 if route == 'sub2api' else 1,
                      'order': ['C', 'A', 'B', 'D'], 'marker': 'fixed-marker'}
            result['output'] = [{'type': 'message', 'role': 'assistant',
                                 'content': [{'type': 'output_text', 'text': json.dumps(answer)}]}]
            return result, info
        self.backend.execute = answer
        with patch('fenjue.service.secrets.randbelow', return_value=123), patch('fenjue.service.secrets.token_hex', return_value='fixed-marker'):
            r = await self.client.post('/api/check', json={'effort': 'high'}, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.backend.calls), 2)
        self.assertFalse(r.json()['routes']['codex_http']['passed'])
        self.assertTrue(r.json()['routes']['sub2api']['passed'])
        repeated = await self.client.post('/api/check', json={'effort': 'high'}, headers=self.auth)
        self.assertEqual(repeated.status_code, 429)
        selected = await self.client.post('/v1/responses', json=self.payload, headers=self.auth)
        self.assertEqual(selected.headers['x-fenjue-route'], 'sub2api')
        self.assertEqual(len(self.backend.calls), 3)
        for p in Path(self.tmp.name).iterdir():
            self.assertNotIn('OPAQUE', p.read_text())
            self.assertNotIn('test-secret', p.read_text())

    async def test_forged_affinity_and_upstream_mismatch_fail_closed(self):
        bad = await self.client.post('/v1/responses', json=self.payload,
                                     headers=self.auth | {'X-Fenjue-Session': 'forged'})
        self.assertEqual(bad.status_code, 409)
        original = self.backend.execute
        async def mismatch(route, payload):
            result, info = await original(route, payload)
            result['reasoning'] = {'effort': 'low'}
            return result, info
        self.backend.execute = mismatch
        r = await self.client.post('/v1/responses', json=self.payload, headers=self.auth)
        self.assertEqual(r.status_code, 502)
        self.assertEqual(len(self.backend.calls), 1)


class SelectionTests(unittest.TestCase):
    def test_checks_expire_and_never_transfer_between_accounts_or_efforts(self):
        from fenjue.service import choose_route
        identity = {'account_ref': 'a', 'controls_version': 'v'}
        report = {'identity': identity, 'time': time.time(), 'model': 'gpt-6-astra', 'effort': 'high',
                  'routes': {'codex_http': {'passed': False}, 'sub2api': {'passed': True}}}
        self.assertEqual(choose_route(report, identity, 'gpt-6-astra', 'high'), 'sub2api')
        self.assertEqual(choose_route(report, identity, 'gpt-6-astra', 'xhigh'), 'codex_http')
        self.assertEqual(choose_route(report, identity | {'account_ref': 'b'}, 'gpt-6-astra', 'high'), 'codex_http')
        self.assertEqual(choose_route(report | {'time': 0}, identity, 'gpt-6-astra', 'high'), 'codex_http')
        report['routes']['sub2api']['passed'] = False
        with self.assertRaises(ValueError):
            choose_route(report, identity, 'gpt-6-astra', 'high')
