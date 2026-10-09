import json
import unittest
import asyncio
from unittest.mock import patch
import httpx
from websockets.asyncio.server import serve
from fenjue.discovery.transport import CallResult


class AlternateTests(unittest.IsolatedAsyncioTestCase):
    async def test_ws_redirect_does_not_contact_destination(self):
        from fenjue.discovery.alternate import websocket_call
        seen = []
        async def destination(ws):
            seen.append(ws.request.headers.get('Authorization'))
            await ws.close()
        async with serve(destination, '127.0.0.1', 0) as target:
            target_url = f'ws://127.0.0.1:{target.sockets[0].getsockname()[1]}/responses'
            def redirect(connection, request):
                response = connection.respond(302, 'redirect')
                response.headers['Location'] = target_url
                return response
            async with serve(destination, '127.0.0.1', 0, process_request=redirect) as origin:
                url = f'ws://127.0.0.1:{origin.sockets[0].getsockname()[1]}/responses'
                result = await websocket_call(url, {'Authorization':'Bearer fixture-only'},
                                              {'model':'gpt-6-astra'}, timeout=2)
        self.assertEqual(seen, [])
        self.assertEqual(result.status, 'handshake_error')
        self.assertEqual(result.http_status, 302)

    async def test_ws_uses_create_and_preserves_done_state_and_terminal_identity(self):
        from fenjue.discovery.alternate import websocket_call
        seen = []
        async def endpoint(ws):
            seen.append(json.loads(await ws.recv()))
            item = {'id': 'm1', 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'ok'}]}
            await ws.send(json.dumps({'type': 'response.output_item.done', 'output_index': 0, 'item': item}))
            await ws.send(json.dumps({'type': 'response.completed', 'response': {'id': 'r1', 'object': 'response',
                'status': 'completed', 'model': 'gpt-6-astra', 'reasoning': {'effort': 'max'}, 'output': []}}))
        async with serve(endpoint, '127.0.0.1', 0) as server:
            url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/responses'
            payload = {'model': 'gpt-6-astra', 'reasoning': {'effort': 'max'}, 'input': [], 'stream': True, 'store': False}
            result = await websocket_call(url, {}, payload, timeout=2)
        self.assertTrue(result.ok)
        self.assertEqual(result.answer, 'ok')
        self.assertEqual(seen[0]['type'], 'response.create')
        self.assertTrue(seen[0]['stream'])
        self.assertEqual(result.response['id'], 'r1')

    async def test_ws_disconnect_is_not_completion_and_never_reconnects(self):
        from fenjue.discovery.alternate import websocket_call
        seen = []
        async def endpoint(ws):
            seen.append(await ws.recv())
            await ws.close()
        async with serve(endpoint, '127.0.0.1', 0) as server:
            url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/responses'
            result = await websocket_call(url, {}, {'model':'gpt-6-astra','reasoning':{'effort':'max'}}, timeout=2)
        self.assertFalse(result.ok)
        self.assertEqual(len(seen), 1)

    async def test_slow_gateway_without_pong_keeps_inference_alive(self):
        from fenjue.discovery.alternate import websocket_call
        from websockets.asyncio.client import connect as real_connect
        from websockets.frames import OP_PING
        async def endpoint(ws):
            original = ws.protocol.recv_frame
            ws.protocol.recv_frame = lambda frame: None if frame.opcode == OP_PING else original(frame)
            await ws.recv()
            await asyncio.sleep(.15)
            await ws.send(json.dumps({'type':'response.completed','response':{
                'id':'r1','object':'response','status':'completed','model':'gpt-6-astra',
                'reasoning':{'effort':'max'},'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'ok'}]}]}}))
        def faster_heartbeat(*args, **kwargs):
            kwargs.setdefault('ping_interval',.02)
            kwargs['ping_timeout']=.02
            return real_connect(*args,**kwargs)
        async with serve(endpoint,'127.0.0.1',0) as server:
            url=f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/responses'
            with patch('fenjue.discovery.alternate.connect',side_effect=faster_heartbeat):
                result=await websocket_call(url,{}, {'model':'gpt-6-astra','reasoning':{'effort':'max'}},timeout=2)
        self.assertTrue(result.ok,result.status)

    async def test_bps_translates_only_documented_fields_and_rejects_tools(self):
        from fenjue.discovery.alternate import bps_payload
        payload = {'model':'gpt-6-astra','reasoning':{'effort':'max'},'instructions':'Keep exact text',
                   'input':[{'role':'user','content':'  sample  '}],'stream':True,'store':False}
        translated = bps_payload(payload)
        self.assertEqual(translated['model_selection'], 'explicit')
        self.assertEqual(translated['reasoning_effort'], 'max')
        self.assertEqual(translated['input'], [{'role':'developer','content':'Keep exact text'},{'role':'user','content':'  sample  '}])
        with self.assertRaises(ValueError):
            bps_payload(payload | {'tools':[{'type':'function','name':'read'}]})
