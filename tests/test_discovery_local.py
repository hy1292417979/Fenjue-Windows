import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from fenjue.discovery.local import LocalAccountSource, controls_fingerprint
from tests.test_discovery_study import config


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        (root/'private-settings.json').write_text(json.dumps({'env':{
            'ADMIN_EMAIL':'fixture','ADMIN_PASSWORD':'private-pass'}}))
        self.source=LocalAccountSource(config()|{'local_install':str(root)})
        self.source.client.close();self.requests=[]
        def handle(request):
            self.requests.append((request.method,request.url.path))
            if request.url.path.endswith('/login'):
                value={'access_token':'admin-token'}
            elif request.method=='DELETE':value={}
            else:value={'id':99,'key':'sk-local-private'}
            return httpx.Response(200,json={'code':0,'data':value})
        self.source.client=httpx.Client(transport=httpx.MockTransport(handle))
    def tearDown(self):self.source.close();self.tmp.cleanup()
    def test_cleanup_even_when_body_raises(self):
        with patch.object(self.source,'sql',return_value=True):
            with self.assertRaises(RuntimeError):
                with self.source.temporary_key():
                    self.assertEqual(self.source.request('sub2api')[0],'http://127.0.0.1:18080/v1/responses')
                    raise RuntimeError('fixture error')
        self.assertEqual(self.requests[-1],('DELETE','/api/v1/keys/99'))
        self.assertIsNone(self.source.key)
        self.assertTrue(self.source.cleanup_result['verified'])
    def test_wrong_usage_binding_is_rejected(self):
        self.source.key_id=99
        row=dict(id=2,account_id=2,group_id=8,requested_model='gpt-6-astra',upstream_model='gpt-6-astra',
                 upstream_response_model='gpt-6-astra',upstream_model_mismatch=False,
                 requested_reasoning_effort='high',reasoning_effort='high')
        with patch.object(self.source,'sql',return_value=[row]):
            self.assertFalse(self.source.audit('sub2api')['verified'])
    def test_usage_telemetry_not_routing_identity(self):
        a={'extra':{'codex_5h_used_percent':2,'codex_usage_updated_at':'old',
                    'openai_oauth_responses_websockets_v2_mode':'off'}}
        b={'extra':a['extra']|{'codex_5h_used_percent':3,'codex_usage_updated_at':'new'}}
        self.assertEqual(controls_fingerprint(a,{},{}),controls_fingerprint(b,{},{}))
        b['extra']['openai_oauth_responses_websockets_v2_mode']='on'
        self.assertNotEqual(controls_fingerprint(a,{},{}),controls_fingerprint(b,{},{}))
    def test_cleanup_query_serializes_postgres_boolean(self):
        queries=[]
        def sql(query):
            queries.append(query)
            return True
        with patch.object(self.source,'sql',side_effect=sql):
            with self.source.temporary_key():pass
        self.assertIn('to_json(',queries[-1])
