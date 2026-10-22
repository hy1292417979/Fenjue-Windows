import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fenjue.cockpit_guard import create_app, Ledger, Backend, busy_code
from tests.test_cockpit_guard import Source, output


def sse(*events):
    return ''.join('data: ' + json.dumps(e) + '\n\n' for e in events)


OVERLOADED = {'type': 'response.failed', 'response': {'id': 'r0', 'status': 'failed', 'error': {
    'code': 'server_is_overloaded', 'message': 'Selected model is at capacity. Please try a different model.'}}}
COMPLETED = {'type': 'response.completed', 'response': output()}


class BusyRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.delays = patch.object(Backend, 'busy_delays', (0, 0, 0))
        self.delays.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.source = Source()
        self.requests, self.script = [], []

        def upstream(request):
            self.requests.append(request)
            step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
            if isinstance(step, Exception):
                raise step
            return step

        self.app = create_app(self.source, 'GATEWAY_KEY', Path(self.tmp.name), httpx.MockTransport(upstream))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url='http://127.0.0.1:17878')
        self.auth = {'Authorization': 'Bearer COCKPIT_KEY', 'session_id': 'codex-session'}
        self.payload = {'model': 'test-model', 'input': 'hello', 'reasoning': {'effort': 'high'}}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.tmp.cleanup()
        self.delays.stop()

    def stream(self, *events, headers=None):
        return httpx.Response(200, content=sse(*events), headers={'Content-Type': 'text/event-stream'} | (headers or {}))

    def calls(self):
        with Ledger(Path(self.tmp.name)).db() as db:
            return [(status, json.loads(meta)) for status, meta in db.execute('SELECT status, metadata FROM calls')]

    async def test_capacity_event_is_resubmitted_unchanged_then_succeeds(self):
        self.script = [self.stream(OVERLOADED), self.stream(OVERLOADED), self.stream(COMPLETED)]
        r = await self.client.post('/v1/responses', json=self.payload | {'stream': True}, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('response.completed', r.text)
        self.assertNotIn('server_is_overloaded', r.text)
        self.assertEqual(len(self.requests), 3)
        bodies = {req.content for req in self.requests}
        self.assertEqual(len(bodies), 1, 'retries must resend the identical request')
        self.assertEqual({req.headers['authorization'] for req in self.requests}, {'Bearer SECRET_OAUTH'})
        [(status, meta)] = self.calls()
        self.assertEqual(status, 'completed')
        self.assertEqual(meta['upstream_attempts'], 3)
        self.assertEqual(meta['busy_retries'], ['server_is_overloaded'] * 2)

    async def test_http_503_and_connect_errors_are_retried(self):
        self.script = [httpx.ConnectError('down'), httpx.ConnectTimeout('slow'),
                       httpx.Response(503, json={'error': {'code': 'server_is_overloaded', 'message': 'busy'}}),
                       httpx.Response(200, json=output())]
        r = await self.client.post('/v1/responses', json=self.payload, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.calls()[0][1]['busy_retries'],
                         ['upstream_unreachable', 'upstream_unreachable', 'server_is_overloaded'])

    async def test_exhausted_stream_reports_openai_capacity_code(self):
        self.script = [self.stream(OVERLOADED)]
        r = await self.client.post('/v1/responses', json=self.payload | {'stream': True}, headers=self.auth)
        self.assertEqual(len(self.requests), 4)
        # Either a 503 before headers, or an OpenAI-shaped response.failed in-stream.
        self.assertTrue(r.status_code == 503 or 'response.failed' in r.text, r.text)
        self.assertIn('server_is_overloaded', r.text)
        self.assertNotIn('response.completed', r.text)
        [(status, meta)] = self.calls()
        self.assertEqual(status, 'failed_upstream_busy')
        self.assertEqual(meta['upstream_attempts'], 4)

    async def test_exhausted_before_headers_returns_503_with_code(self):
        self.script = [httpx.Response(503, json={'error': {'code': 'server_is_overloaded', 'message': 'busy'}})]
        r = await self.client.post('/v1/responses', json=self.payload, headers=self.auth)
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()['error']['code'], 'server_is_overloaded')
        self.assertEqual(len(self.requests), 4)

    async def test_non_busy_failures_are_never_retried(self):
        cases = [httpx.Response(400, json={'error': {'code': 'invalid_request'}}),
                 httpx.Response(429, json={'error': {'type': 'usage_limit_reached', 'message': 'limit'}}),
                 httpx.Response(401, json={'error': {'code': 'token_expired'}}),
                 self.stream({'type': 'response.failed', 'response': {'error': {'code': 'context_length_exceeded'}}})]
        for case in cases:
            self.requests.clear()
            self.script = [case]
            r = await self.client.post('/v1/responses', json=self.payload | {'stream': True}, headers=self.auth)
            self.assertNotIn('response.completed', r.text)
            self.assertEqual(len(self.requests), 1, case)

    async def test_failure_after_output_started_is_not_retried(self):
        item = {'type': 'message', 'id': 'm1', 'role': 'assistant', 'content': []}
        self.script = [self.stream({'type': 'response.output_item.done', 'output_index': 0, 'item': item}, OVERLOADED)]
        await self.client.post('/v1/responses', json=self.payload | {'stream': True}, headers=self.auth)
        self.assertEqual(len(self.requests), 1)

    async def test_retry_keeps_turn_state_codex_already_received(self):
        self.script = [self.stream(OVERLOADED, headers={'x-codex-turn-state': 'first'}),
                       self.stream(COMPLETED, headers={'x-codex-turn-state': 'second'})]
        r = await self.client.post('/v1/responses', json=self.payload | {'stream': True}, headers=self.auth)
        self.assertIn('response.completed', r.text)
        self.assertEqual(r.headers['x-codex-turn-state'], 'first')
        self.assertNotIn('x-codex-turn-state', self.requests[0].headers)
        self.assertEqual(self.requests[1].headers['x-codex-turn-state'], 'first')

    async def test_busy_retries_setting_can_disable(self):
        settings = self.source.settings
        self.source.settings = lambda: settings() | {'busy_retries': 0}
        self.script = [self.stream(OVERLOADED)]
        await self.client.post('/v1/responses', json=self.payload | {'stream': True}, headers=self.auth)
        self.assertEqual(len(self.requests), 1)


class BusyCodeTests(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(busy_code(OVERLOADED), 'server_is_overloaded')
        self.assertEqual(busy_code({'type': 'error', 'code': 'slow_down'}), 'slow_down')
        self.assertEqual(busy_code({'error': {'message': 'Selected model is at capacity.'}}), 'server_is_overloaded')
        self.assertIsNone(busy_code({'error': {'code': 'usage_limit_reached', 'message': 'limit'}}))
        self.assertIsNone(busy_code(['x']))


if __name__ == '__main__':
    unittest.main()


class InStreamFailureShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_after_headers_is_response_failed_with_code(self):
        # Force the "headers already sent" path with a slow upstream body.
        import asyncio
        from fenjue import cockpit_guard
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        class Slow(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.sleep(0.3)
                yield sse(OVERLOADED).encode()

        def upstream(request):
            return httpx.Response(200, stream=Slow(), headers={'Content-Type': 'text/event-stream'})

        with patch.object(Backend, 'busy_delays', (0,)):
            app = create_app(Source(), 'GATEWAY_KEY', Path(tmp.name), httpx.MockTransport(upstream))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1:17878') as c:
                r = await c.post('/v1/responses', json={'model': 'test-model', 'input': 'hi', 'stream': True,
                                 'reasoning': {'effort': 'high'}}, headers={'Authorization': 'Bearer COCKPIT_KEY'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('event: response.failed', r.text)
        failed = json.loads(r.text.split('event: response.failed\ndata: ')[1].split('\n')[0])
        self.assertEqual(failed['response']['error']['code'], 'server_is_overloaded')
