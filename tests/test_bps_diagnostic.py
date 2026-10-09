import json
import unittest
import httpx


class BpsDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_policy_error_is_preserved_without_credential_echo(self):
        from fenjue.discovery.bps_diagnostic import diagnose_http
        seen=[]
        async def handler(request):
            seen.append(request)
            return httpx.Response(403, json={'detail':'This request was blocked by our usage policy. secret-token'},
                                  headers={'x-request-id':'test-request','set-cookie':'private-cookie'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            record=await diagnose_http(client, {'body':{'model':'gpt-6-astra','reasoning_effort':'medium'}},
                                       {'Authorization':'Bearer secret-token'}, ['secret-token'])
        self.assertEqual(record['http_status'],403)
        self.assertIn('blocked by our usage policy',record['error_body']['detail'])
        self.assertNotIn('secret-token',json.dumps(record))
        self.assertNotIn('private-cookie',json.dumps(record))
        self.assertEqual(len(seen),1)
        self.assertEqual(str(seen[0].url),'https://bps.openai.com/basispoints/api/responses')

    async def test_redirect_not_followed_and_not_marked_success(self):
        from fenjue.discovery.bps_diagnostic import diagnose_http
        seen=[]
        async def handler(request):
            seen.append(request)
            return httpx.Response(302,headers={'location':'https://example.invalid/collect'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=True) as client:
            record=await diagnose_http(client, {'body':{'model':'gpt-6-astra','reasoning_effort':'medium'}}, {}, [])
        self.assertEqual(record['status'],'http_error')
        self.assertEqual(len(seen),1)

    async def test_completed_response_evidence_retains_answer_not_opaque_output(self):
        from fenjue.discovery.bps_diagnostic import diagnose_http
        async def handler(request):
            return httpx.Response(200,json={'id':'r1','object':'response','status':'completed',
                'model':'gpt-6-astra','reasoning':{'effort':'medium'},'output':[
                  {'type':'reasoning','encrypted_content':'private-opaque'},
                  {'type':'message','role':'assistant','content':[{'type':'output_text','text':'{"product":11051}'}]}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            record=await diagnose_http(client, {'body':{'model':'gpt-6-astra','reasoning_effort':'medium'}}, {}, [])
        self.assertEqual(record['status'],'completed')
        self.assertEqual(record['answer'],'{"product":11051}')
        self.assertNotIn('private-opaque',json.dumps(record))
