"""python -m unittest test_retry_proxy  （在 retry_proxy 目录下运行）"""
import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import control
import proxy

CONFIG = '''model_reasoning_effort = "medium"\r
model = "gpt-6-astra"\r
model_provider = "codex_local_access"\r
[model_providers.custom]\r
name = "52API"\r
base_url = "https://example.invalid"\r
\r
[model_providers.codex_local_access]\r
name = "Codex API Service"\r
base_url = "http://localhost:50555/v1"\r
wire_api = "responses"\r
requires_openai_auth = false\r
experimental_bearer_token = "COCKPIT_TOKEN"\r
supports_websockets = false\r
http_headers = { x-openai-actor-authorization = "ACTOR", x-cockpit-instance-id = ".codex" }\r
\r
[projects.'d:\\users\\admin\\my document\\荒笛']\r
trust_level = "trusted"\r
'''


def sse(*events):
    return ''.join('data: ' + json.dumps(e) + '\n\n' for e in events)


CREATED = {'type': 'response.created', 'response': {'id': 'r1', 'status': 'in_progress'}}
OVERLOADED = {'type': 'response.failed', 'response': {'id': 'r1', 'status': 'failed', 'error': {
    'code': 'server_is_overloaded', 'message': 'Selected model is at capacity. Please try a different model.'}}}
DELTA = {'type': 'response.output_text.delta', 'delta': 'hi'}
DONE = {'type': 'response.completed', 'response': {'id': 'r2', 'status': 'completed', 'output': []}}


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = root / 'config.toml'
        self.config.write_bytes(CONFIG.encode())
        self.patches = [patch.object(proxy, 'STATE', root / 'state.json'), patch.object(proxy, 'LOG', root / 'proxy.log'),
                        patch.object(control, 'STATE', root / 'state.json'), patch.object(control, 'LOG', root / 'proxy.log')]
        for p in self.patches:
            p.start()
        proxy._cache.clear()
        self.requests, self.script = [], []

        def upstream(request):
            self.requests.append(request)
            step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
            if isinstance(step, Exception):
                raise step
            return step

        self.app = proxy.create_app(httpx.MockTransport(upstream), delays=(0, 0, 0), config_path=self.config)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url='http://127.0.0.1:50556')

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def stream(self, *events, headers=None):
        return httpx.Response(200, content=sse(*events), headers={'Content-Type': 'text/event-stream'} | (headers or {}))

    async def post(self, **extra):
        return await self.client.post('/v1/responses', json={'model': 'gpt-6-astra', 'input': 'hi', 'stream': True},
                                      headers={'session_id': 's1', 'originator': 'codex_desktop'} | extra)


class ProxyTests(Base):
    async def test_forwards_to_cockpit_with_its_credentials(self):
        self.script = [self.stream(CREATED, DELTA, DONE)]
        r = await self.post()
        self.assertEqual(r.status_code, 200)
        req = self.requests[0]
        self.assertEqual(str(req.url), 'http://localhost:50555/v1/responses')
        self.assertEqual(req.headers['authorization'], 'Bearer COCKPIT_TOKEN')
        self.assertEqual(req.headers['x-openai-actor-authorization'], 'ACTOR')
        self.assertEqual(req.headers['session_id'], 's1')
        self.assertEqual(r.text, sse(CREATED, DELTA, DONE))

    async def test_overloaded_before_output_is_retried_identically(self):
        self.script = [self.stream(CREATED, OVERLOADED), self.stream(CREATED, OVERLOADED), self.stream(CREATED, DELTA, DONE)]
        r = await self.post()
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(len({q.content for q in self.requests}), 1)
        self.assertNotIn('server_is_overloaded', r.text.replace(': retry-proxy: server_is_overloaded', ''))
        self.assertIn('response.completed', r.text)
        self.assertEqual(r.text.count('response.created'), 1, 'Codex must see exactly one response.created')
        log = (Path(self.tmp.name) / 'proxy.log').read_text(encoding='utf-8')
        self.assertIn('"outcome": "ok"', log)

    async def test_http_busy_and_unreachable_are_retried(self):
        self.script = [httpx.ConnectError('down'),
                       httpx.Response(503, json={'error': {'code': 'server_is_overloaded'}}),
                       self.stream(CREATED, DONE)]
        r = await self.post()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.requests), 3)

    async def test_exhausted_passes_original_overload_to_codex(self):
        self.script = [self.stream(CREATED, OVERLOADED)]
        r = await self.post()
        self.assertEqual(len(self.requests), 4)
        self.assertIn('server_is_overloaded', r.text)
        self.assertIn('at capacity', r.text)

    async def test_exhausted_http_returns_original_status(self):
        self.script = [httpx.Response(503, json={'error': {'code': 'server_is_overloaded', 'message': 'busy'}})]
        r = await self.post()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()['error']['code'], 'server_is_overloaded')
        self.assertEqual(len(self.requests), 4)

    async def test_non_busy_errors_pass_through_untouched_and_once(self):
        for resp in (httpx.Response(429, json={'error': {'type': 'usage_limit_reached', 'resets_in_seconds': 60}},
                                    headers={'x-codex-primary-used-percent': '100'}),
                     httpx.Response(401, json={'error': {'code': 'token_expired'}}),
                     httpx.Response(400, json={'error': {'code': 'context_length_exceeded'}})):
            self.requests.clear()
            self.script = [resp]
            r = await self.post()
            self.assertEqual(r.status_code, resp.status_code)
            self.assertEqual(r.json(), resp.json())
            self.assertEqual(len(self.requests), 1)
        self.assertEqual(r.status_code, 400)

    async def test_usage_limit_headers_preserved(self):
        self.script = [httpx.Response(429, json={'error': {'type': 'usage_limit_reached'}},
                                      headers={'x-codex-primary-used-percent': '100'})]
        r = await self.post()
        self.assertEqual(r.headers['x-codex-primary-used-percent'], '100')

    async def test_failure_after_output_started_is_not_retried(self):
        self.script = [self.stream(CREATED, DELTA, OVERLOADED)]
        r = await self.post()
        self.assertEqual(len(self.requests), 1)
        self.assertIn('server_is_overloaded', r.text)

    async def test_other_failed_codes_not_retried(self):
        self.script = [self.stream(CREATED, {'type': 'response.failed', 'response': {'error': {'code': 'context_length_exceeded'}}})]
        await self.post()
        self.assertEqual(len(self.requests), 1)

    async def test_retry_reuses_first_turn_state(self):
        self.script = [self.stream(CREATED, OVERLOADED, headers={'x-codex-turn-state': 'T1'}),
                       self.stream(CREATED, DONE, headers={'x-codex-turn-state': 'T2'})]
        r = await self.post()
        self.assertEqual(r.headers['x-codex-turn-state'], 'T1')
        self.assertEqual(self.requests[1].headers['x-codex-turn-state'], 'T1')

    async def test_retry_ending_in_usage_limit_is_reported_in_stream(self):
        self.script = [self.stream(CREATED, OVERLOADED),
                       httpx.Response(429, json={'error': {'code': 'usage_limit_reached', 'message': 'limit'}})]
        r = await self.post()
        self.assertIn('response.failed', r.text)
        self.assertIn('usage_limit_reached', r.text)
        self.assertEqual(len(self.requests), 2)

    async def test_models_and_compact_paths(self):
        self.script = [httpx.Response(200, json={'data': []})]
        r = await self.client.get('/v1/models?client_version=1')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(str(self.requests[-1].url), 'http://localhost:50555/v1/models?client_version=1')
        self.script = [httpx.Response(503, json={'error': {'code': 'server_is_overloaded'}}),
                       httpx.Response(200, json={'object': 'response.compaction', 'output': []})]
        r = await self.client.post('/v1/responses/compact', json={'model': 'm', 'input': []})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(str(self.requests[-1].url), 'http://localhost:50555/v1/responses/compact')

    async def test_follows_cockpit_port_and_token_changes(self):
        self.script = [self.stream(CREATED, DONE)]
        await self.post()
        text = self.config.read_text(encoding='utf-8').replace('50555', '50999').replace('COCKPIT_TOKEN', 'NEW_TOKEN')
        self.config.write_text(text + '\n# touched\n', encoding='utf-8')
        await self.post()
        self.assertEqual(self.requests[-1].url.port, 50999)
        self.assertEqual(self.requests[-1].headers['authorization'], 'Bearer NEW_TOKEN')

    async def test_health(self):
        r = await self.client.get('/__retry_proxy/health')
        self.assertEqual(r.json()['upstream_base'], 'http://localhost:50555/v1')


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = root / 'config.toml'
        self.config.write_bytes(CONFIG.encode())
        self.state = patch.object(control, 'STATE', root / 'state.json')
        self.state.start()

    def tearDown(self):
        self.state.stop()
        self.tmp.cleanup()

    def test_on_off_round_trip_only_touches_provider_line(self):
        control.point_codex_to_proxy(self.config)
        on = self.config.read_bytes().decode()
        cfg = tomllib.loads(on)
        self.assertEqual(cfg['model_provider'], 'codex_retry')
        self.assertEqual(cfg['model_providers']['codex_retry']['base_url'], 'http://127.0.0.1:50556/v1')
        self.assertEqual(cfg['model_providers']['codex_local_access'],
                         tomllib.loads(CONFIG)['model_providers']['codex_local_access'])
        self.assertEqual(cfg['projects'], tomllib.loads(CONFIG)['projects'])
        self.assertNotIn('\n\n\n', on.replace('\r', ''))
        self.assertIn('\r\n', on)
        self.assertEqual(json.loads((Path(self.tmp.name) / 'state.json').read_text())['upstream'], 'codex_local_access')
        self.assertEqual((Path(self.tmp.name) / 'config.toml.bak_retry_proxy').read_bytes(), CONFIG.encode())
        # Second "on" is idempotent and keeps the real upstream
        control.point_codex_to_proxy(self.config)
        self.assertEqual(self.config.read_bytes().decode(), on)
        self.assertEqual(json.loads((Path(self.tmp.name) / 'state.json').read_text())['upstream'], 'codex_local_access')
        control.point_codex_to_cockpit(self.config)
        off = tomllib.loads(self.config.read_bytes().decode())
        self.assertEqual(off['model_provider'], 'codex_local_access')
        expected = CONFIG.encode() + control.SECTION.replace('\n', '\r\n').encode()
        self.assertEqual(self.config.read_bytes().replace(b'\r\n\r\n[model_providers.codex_retry]', b'\r\n[model_providers.codex_retry]'),
                         expected.replace(b'\r\n\r\n[model_providers.codex_retry]', b'\r\n[model_providers.codex_retry]'))

    def test_off_when_not_on_is_noop(self):
        self.assertFalse(control.point_codex_to_cockpit(self.config))
        self.assertEqual(self.config.read_bytes(), CONFIG.encode())

    def test_refuses_unknown_upstream(self):
        self.config.write_text(CONFIG.replace('model_provider = "codex_local_access"', 'model_provider = "missing"'))
        with self.assertRaises(RuntimeError):
            control.point_codex_to_proxy(self.config)


if __name__ == '__main__':
    unittest.main()
