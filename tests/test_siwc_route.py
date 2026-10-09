import hashlib
import json
import time
import unittest

import httpx

from fenjue.discovery.siwc import SiwcSource
from fenjue.siwc_login import DIRECT_SCOPE, ISSUER


class SiwcRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.expected = hashlib.sha256(b'fixture-subject').hexdigest()
        self.saved = dict(issuer=ISSUER, subject='fixture-subject', subject_hash=self.expected,
                          access_token='fixture-token', scopes=[DIRECT_SCOPE], expires_at=time.time()+600)

    def test_identity_scope_and_expiry_required(self):
        for changes in ({'subject': 'another'}, {'issuer': 'https://example.test'},
                        {'scopes': []}, {'expires_at': 1}, {'access_token': ''}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                SiwcSource(self.saved | changes, self.expected)

    async def test_same_token_catalog_and_completed_response(self):
        seen = []
        def handle(request):
            seen.append(request)
            self.assertEqual(request.headers['authorization'], 'Bearer fixture-token')
            if request.method == 'GET':
                return httpx.Response(200, json={'models': [{'slug':'fixture-model', 'visibility':'list'}]})
            payload = json.loads(request.content)
            self.assertFalse(payload['store'])
            self.assertTrue(payload['stream'])
            response = {'status':'completed', 'model':'fixture-model', 'reasoning':{'effort':'high'},
                        'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'42'}]}]}
            return httpx.Response(200, headers={'content-type':'text/event-stream'},
                                  text='data: '+json.dumps({'type':'response.completed','response':response})+'\n\n')
        source = SiwcSource(self.saved, self.expected)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            models = await source.catalog(client)
            result = await source.call(client, {'model':'fixture-model', 'input':[{'role':'user','content':'task'}],
                                               'reasoning':{'effort':'high'}, 'store':False, 'stream':True}, models)
            self.assertTrue(result.ok)
            self.assertEqual(result.answer, '42')
        self.assertEqual([str(x.url) for x in seen], ['https://api.openai.com/v1/models','https://api.openai.com/v1/responses'])

    async def test_unavailable_model_stops_before_inference(self):
        source = SiwcSource(self.saved, self.expected)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: self.fail('unexpected network request'))) as client:
            with self.assertRaises(ValueError):
                await source.call(client, {'model':'unlisted'}, ['other'])

    async def test_catalog_redirect_is_not_followed(self):
        seen = []
        def handle(request):
            seen.append(request)
            return httpx.Response(302, headers={'location':'https://example.test'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
            with self.assertRaises(ValueError):
                await SiwcSource(self.saved, self.expected).catalog(client)
        self.assertEqual(len(seen), 1)
