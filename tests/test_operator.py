import tempfile
import unittest
from pathlib import Path


class OperatorTests(unittest.TestCase):
    def test_switch_preserves_other_settings_and_rollback_preserves_new_telemetry(self):
        from fenjue.operator import switch, restore
        class Source:
            config={'account_id':1}
            def __init__(self):
                self.extra={'openai_oauth_responses_websockets_v2_enabled':False,
                            'openai_oauth_responses_websockets_v2_mode':'off',
                            'unrelated':'preserve','usage_counter':7}
            def api(self, method, path, body=None):
                if method in ('POST', 'PUT'):
                    # A runtime writer updates unrelated fields after the operator's GET.
                    self.extra['usage_counter'] += 1
                    self.extra['concurrent_setting'] = 'new'
                    if method=='PUT':
                        self.extra=dict(body['extra'])
                    else:
                        assert path=='/api/v1/admin/accounts/bulk-update'
                        assert body['account_ids']==[1]
                        self.extra.update(body['extra'])
                return {'extra':dict(self.extra),'id':1}
        source=Source()
        with tempfile.TemporaryDirectory() as d:
            snapshot=Path(d)/'rollback.json'
            switch(source, 'codex_ws', snapshot)
            self.assertEqual(source.extra['openai_oauth_responses_websockets_v2_mode'],'ctx_pool')
            self.assertEqual(source.extra['unrelated'],'preserve')
            self.assertEqual(source.extra['usage_counter'],8)
            self.assertEqual(source.extra['concurrent_setting'],'new')
            source.extra['usage_counter']=12
            restore(source,snapshot)
            self.assertFalse(source.extra['openai_oauth_responses_websockets_v2_enabled'])
            self.assertEqual(source.extra['openai_oauth_responses_websockets_v2_mode'],'off')
            self.assertEqual(source.extra['usage_counter'],13)
            self.assertIsNone(source.extra['openai_ws_force_http'])
            with self.assertRaises(FileExistsError):switch(source,'codex_ws',snapshot)

    def test_restoring_snapshot_for_different_account_is_rejected(self):
        from fenjue.operator import restore
        import json
        class Source:config={'account_id':2}
        with tempfile.TemporaryDirectory() as d:
            snapshot=Path(d)/'rollback.json';snapshot.write_text(json.dumps({'account_id':1,'previous':{}}))
            with self.assertRaises(ValueError):restore(Source(),snapshot)
