import json
import unittest
import httpx
from fenjue.discovery.transport import RouteTransport, scrub
from fenjue.discovery.local import verify_binding


def event(kind='response.completed', **changes):
    response = dict(status='completed', model='gpt-6-astra', reasoning={'effort': 'high'},
                    output=[{'type':'message','role':'assistant','phase':'final_answer',
                             'content':[{'type':'output_text','text':'{"ok":true}'}]}]) | changes
    return 'event: ' + kind + '\ndata: ' + json.dumps({'type':kind,'response':response}) + '\n\n'


class Source:
    config={'timeout_seconds':2}
    def request(self, route):
        return 'https://fixture.test/responses', {'Authorization':'Bearer private'}


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, text, status=200):
        async def handler(request):
            self.body=json.loads(request.content)
            return httpx.Response(status, text=text, headers={'content-type':'text/event-stream'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await RouteTransport(Source(), client).call('sub2api',
                {'model':'gpt-6-astra','reasoning':{'effort':'high'},'input':[], 'instructions':'fixed'})
    async def test_complete_and_payload(self):
        result=await self.call(event().rstrip())
        self.assertTrue(result.ok);self.assertEqual(result.answer,'{"ok":true}')
        self.assertEqual(self.body['instructions'],'fixed')
        self.assertEqual(result.output[0]['phase'],'final_answer')
    async def test_multiline_event(self):
        text=event().replace('"response":', '\ndata: "response":')
        self.assertTrue((await self.call(text)).ok)
    async def test_final_phase_excludes_commentary_from_answer(self):
        output=[{'type':'message','role':'assistant','phase':'commentary','content':[{'type':'output_text','text':'Working'}]},
                {'type':'message','role':'assistant','phase':'final_answer','content':[{'type':'output_text','text':'{"ok":true}'}]}]
        self.assertEqual((await self.call(event(output=output))).answer,'{"ok":true}')
    async def test_failure_never_overwritten(self):
        for text in ('data: [DONE]\n\n', event(status='incomplete'),
                     event('response.failed')+event(), event(model='other'),
                     event(reasoning={'effort':'low'}), 'data: invalid\n\n'):
            self.assertFalse((await self.call(text)).ok)
    async def test_redirect_not_followed(self):
        self.assertFalse((await self.call('',302)).ok)
    async def test_complete_response_json_and_nonresponse_diagnostics(self):
        payload={'model':'gpt-6-astra','reasoning':{'effort':'high'}}
        good=json.loads(event().split('data: ',1)[1])['response']|{'object':'response'}
        for body,expected in ((good,True),({'error':{'message':'private'}},False),
                              (good|{'status':'incomplete'},False),(good|{'object':'other'},False)):
            async def handler(request):return httpx.Response(200,json=body)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result=await RouteTransport(Source(),client).call('codex_http',payload)
                self.assertEqual(result.ok,expected)
                self.assertNotIn('private',json.dumps(result.record()))
                if expected:self.assertEqual(result.wire_format,'response_json')
    async def test_unlabelled_sse_requires_real_completed_event(self):
        payload={'model':'gpt-6-astra','reasoning':{'effort':'high'}}
        for body,expected in ((event(),True),(event('response.failed')+event(),False),
                              ('data: [DONE]\n\n',False),('<html>not a response</html>',False)):
            async def handler(request):return httpx.Response(200,content=body.encode())
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                result=await RouteTransport(Source(),client).call('codex_http',payload)
                self.assertEqual(result.ok,expected)
                if expected:self.assertEqual(result.wire_format,'sse_unlabelled')
    async def test_done_items_fill_empty_terminal_output(self):
        item={'id':'fc1','type':'function_call','namespace':'fixture','name':'read_record',
              'call_id':'call1','arguments':'{"record_id":"root"}'}
        prefix='data: '+json.dumps({'type':'response.output_item.done','output_index':0,'item':item})+'\n\n'
        result=await self.call(prefix+event(output=[]))
        self.assertTrue(result.ok)
        self.assertEqual(result.output,[item])
        self.assertEqual(result.diagnostic['output_source'],'output_item_done')
        self.assertFalse((await self.call(event(output=[]))).ok)
    async def test_conflicting_stream_and_terminal_items_rejected(self):
        item={'id':'m1','type':'message','role':'assistant','content':[{'type':'output_text','text':'first'}]}
        prefix='data: '+json.dumps({'type':'response.output_item.done','output_index':0,'item':item})+'\n\n'
        terminal=item|{'content':[{'type':'output_text','text':'other'}]}
        self.assertFalse((await self.call(prefix+event(output=[terminal]))).ok)
    async def test_nonempty_terminal_retains_opaque_fields_from_done(self):
        reasoning={'id':'r1','type':'reasoning','encrypted_content':'opaque-state','summary':[]}
        message={'id':'m1','type':'message','role':'assistant','content':[{'type':'output_text','text':'ok'}]}
        prefix='data: '+json.dumps({'type':'response.output_item.done','output_index':0,'item':reasoning})+'\n\n'
        stripped={k:v for k,v in reasoning.items() if k!='encrypted_content'}
        result=await self.call(prefix+event(output=[stripped,message]))
        self.assertTrue(result.ok)
        self.assertEqual(result.output[0].get('encrypted_content'),'opaque-state')
        self.assertNotIn('opaque-state',json.dumps(result.record()))
    async def test_split_bytes_and_timeout(self):
        import anyio
        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                for byte in event().encode():yield bytes([byte])
        async def handler(request):
            return httpx.Response(200,stream=Chunks(),headers={'content-type':'text/event-stream'})
        payload={'model':'gpt-6-astra','reasoning':{'effort':'high'}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertTrue((await RouteTransport(Source(),client).call('sub2api',payload)).ok)
        class Slow(httpx.AsyncByteStream):
            async def __aiter__(self):
                await anyio.sleep(.1)
                yield event().encode()
        source=Source();source.config={'timeout_seconds':.01}
        async def slow_handler(request):
            return httpx.Response(200,stream=Slow(),headers={'content-type':'text/event-stream'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow_handler)) as client:
            self.assertEqual((await RouteTransport(source,client).call('sub2api',payload)).status,'timeout')
    def test_scrub_nested(self):
        result=scrub({'answer':'Bearer abcsecret sk-abcdefghijk', 'encrypted_content':'opaque',
                      'x':['abcsecret']}, ['abcsecret'])
        self.assertNotIn('abcsecret',json.dumps(result));self.assertNotIn('opaque',json.dumps(result))
    def test_binding_rejects_pool_and_fallback(self):
        good={'account_id':1,'group_id':8,'members':[1],'fallback_group_id':None,
              'fallback_group_id_on_invalid_request':None,'platform':'openai','type':'oauth',
              'status':'active','group_status':'active','model_routing_enabled':False}
        verify_binding(good,{'account_id':1,'group_id':8})
        for change in ({'members':[1,2]},{'fallback_group_id':9},{'account_id':2},
                       {'model_routing_enabled':True}):
            with self.assertRaises(ValueError):verify_binding(good|change,{'account_id':1,'group_id':8})
