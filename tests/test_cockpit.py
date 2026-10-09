"""Offline regression checks: no requests to actual Cockpit accounts."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from fenjue.cockpit import CockpitSource, create_app


class SourceTests(unittest.TestCase):
    def test_connection_reloads_port_and_key(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {}, clear=True):
            path = Path(tmp) / 'config.json'
            source = CockpitSource(path)
            for port, key in ((64492, 'first'), (64300, 'second')):
                path.write_text(json.dumps({'host': '127.0.0.1', 'port': port, 'api-keys': [key]}))
                self.assertEqual(source.connection(), (f'http://127.0.0.1:{port}', key))

    def test_invalid_and_remote_config_and_multiple_keys_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {}, clear=True):
            path = Path(tmp) / 'config.json'
            good = {'host': '127.0.0.1', 'port': 64492, 'api-keys': ['secret']}
            for value in (None, [], {}, good | {'host': 'evil.test'}, good | {'port': 17877},
                          good | {'port': True}, good | {'api-keys': ['one', 'two']},
                          good | {'api-keys': ['bad\r\nheader']}):
                path.write_text(json.dumps(value))
                with self.assertRaises(HTTPException) as error:
                    CockpitSource(path).connection()
                self.assertNotIn('secret', str(error.exception.detail))
            path.write_text(json.dumps(good | {'api-keys': ['one', 'two']}))
            with patch.dict('os.environ', {'FENJUE_COCKPIT_KEY': 'two'}):
                self.assertEqual(CockpitSource(path).connection()[1], 'two')


class Source:
    def connection(self):
        return 'http://127.0.0.1:64492', 'UPSTREAM_SECRET'


class BrokenStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        yield b'data: {"type":"response.created"}\n\n'
        raise httpx.ReadError('sensitive upstream error')

    async def aclose(self):
        self.closed = True


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.result = httpx.Response(200, json={'id': 'r1', 'status': 'completed', 'output': []})
        def upstream(request):
            self.requests.append(request)
            if isinstance(self.result, Exception):
                raise self.result
            return self.result
        self.app = create_app(Source(), 'LOCAL_SECRET', transport=httpx.MockTransport(upstream))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://127.0.0.1:17877')
        self.headers = {'Authorization': 'Bearer LOCAL_SECRET'}
        self.payload = {'model': 'some-model', 'input': 'hello'}

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_auth_and_origin_gate(self):
        for headers in ({}, self.headers | {'Origin': 'https://evil.test'}, self.headers | {'Host': 'evil.test'}):
            r = await self.client.post('/v1/responses', json=self.payload, headers=headers)
            self.assertIn(r.status_code, (401, 403))
        self.assertEqual(self.requests, [])
        page = await self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('LOCAL_SECRET', page.text)
        self.assertNotIn('UPSTREAM_SECRET', page.text)

    async def test_models_not_inference_and_no_metadata_leak(self):
        self.result = httpx.Response(200, json={'data': [{'id': 'a', 'account': 'secret'}, {'id': 'b'}]})
        r = await self.client.get('/v1/models', headers=self.headers)
        self.assertEqual([m['id'] for m in r.json()['data']], ['a', 'b'])
        self.assertNotIn('secret', r.text)
        self.assertEqual(self.requests[0].method, 'GET')
        self.assertEqual(self.requests[0].url.path, '/v1/models')

    async def test_payload_tools_and_affinity_preserved(self):
        payload = self.payload | {'input': [{'type': 'reasoning', 'encrypted_content': 'OPAQUE'}],
            'tools': [{'type': 'function', 'name': 'lookup'}], 'reasoning': {'effort': 'high'}, 'stream': False}
        raw = json.dumps(payload).encode()
        r = await self.client.post('/v1/responses', content=raw,
                                  headers=self.headers | {'session_id': 'session1', 'x-codex-turn-state': 'state'})
        self.assertEqual(r.status_code, 200, r.text)
        sent = self.requests[0]
        self.assertEqual(sent.content, raw)
        self.assertEqual(sent.headers['session_id'], 'session1')
        self.assertEqual(sent.headers['x-codex-turn-state'], 'state')
        self.assertEqual(sent.headers['authorization'], 'Bearer UPSTREAM_SECRET')

    async def test_stream_preserved_exactly(self):
        raw = b'data: {"type":"response.output_text.delta","delta":"hello"}\n\ndata: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        self.result = httpx.Response(200, content=raw, headers={'Content-Type': 'text/event-stream', 'x-codex-turn-state': 'next'})
        r = await self.client.post('/v1/responses', json=self.payload | {'stream': True}, headers=self.headers)
        self.assertEqual(r.content, raw)
        self.assertEqual(r.headers['x-codex-turn-state'], 'next')

    async def test_error_redacted_and_no_retry_or_redirect(self):
        for code in (302, 401, 429, 500):
            self.requests.clear()
            self.result = httpx.Response(code, text='UPSTREAM_SECRET', headers={'Location': 'https://evil.test/'})
            r = await self.client.post('/v1/responses', json=self.payload, headers=self.headers)
            self.assertEqual(r.status_code, code if code >= 400 else 502)
            self.assertNotIn('UPSTREAM_SECRET', r.text)
            self.assertEqual(len(self.requests), 1)

    async def test_connection_error_no_retry(self):
        self.result = httpx.ConnectError('UPSTREAM_SECRET')
        r = await self.client.post('/v1/responses', json=self.payload, headers=self.headers)
        self.assertEqual(r.status_code, 502)
        self.assertNotIn('UPSTREAM_SECRET', r.text)
        self.assertEqual(len(self.requests), 1)

    async def test_stream_error_and_cleanup(self):
        stream = BrokenStream()
        self.result = httpx.Response(200, stream=stream, headers={'Content-Type': 'text/event-stream'})
        r = await self.client.post('/v1/responses', json=self.payload | {'stream': True}, headers=self.headers)
        self.assertIn('event: error', r.text)
        self.assertNotIn('sensitive', r.text)
        self.assertTrue(stream.closed)
        self.assertEqual(len(self.requests), 1)

    async def test_malformed_and_oversized_payloads_rejected(self):
        for raw in (b'null', b'[]', b'{"model":"a","model":"b","input":"x"}', b'{"model":"a","input":NaN}', b'{}'):
            r = await self.client.post('/v1/responses', content=raw, headers=self.headers)
            self.assertEqual(r.status_code, 400)
        with patch('fenjue.cockpit.MAX_BODY', 16):
            r = await self.client.post('/v1/responses', content=b'x' * 17, headers=self.headers)
            self.assertEqual(r.status_code, 413)
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
