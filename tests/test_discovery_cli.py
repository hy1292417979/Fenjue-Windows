import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from tests.test_discovery_study import config
from fenjue.discovery.__main__ import main
from fenjue.discovery.report import build_report
from fenjue.discovery.study import prepare, open_study


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        cfg=self.root/'config.json';cfg.write_text(json.dumps(config()))
        self.cfg=cfg;self.out=self.root/'study'
    def tearDown(self):self.tmp.cleanup()
    def test_offline_commands_never_load_credentials(self):
        with patch('fenjue.discovery.local.LocalAccountSource',side_effect=AssertionError('credentials loaded')):
            with redirect_stdout(StringIO()):
                self.assertEqual(main(['prepare','--config',str(self.cfg),'--out',str(self.out)]),0)
                for cmd in ('request-diff','report','status'):
                    self.assertEqual(main([cmd,'--study',str(self.out)]),0)
    def test_incomplete_and_ceiling_not_full_capability(self):
        prepare(self.cfg,self.out)
        with open_study(self.out) as s:
            self.assertEqual(build_report(s)['conclusion'],'incomplete_evidence')
            for pair in s.schedule:
                s.db.save_outcome('smoke',pair['key'],pair|{'grade':{'passed':True},'identity_ok':True,'status':'completed'})
            r=build_report(s)
            self.assertEqual(r['conclusion'],'smoke_only')
            self.assertTrue(r['ceiling_effect']);self.assertFalse(r['full_capability_proven'])
    def test_unknown_config_and_model_tamper(self):
        prepare(self.cfg,self.out)
        cfg=self.out/'config.json';value=json.loads(cfg.read_text());value['model']='other'
        cfg.write_text(json.dumps(value))
        with self.assertRaises(ValueError):open_study(self.out)
    def test_protocol_failures_are_not_quality_comparisons(self):
        for failure in ('not_sse','timeout','model_mismatch_or_missing','budget_exhausted'):
            out=self.root/failure
            prepare(self.cfg,out)
            with open_study(out) as s:
                # Exercise screening conclusion logic with a tiny isolated task fixture.
                s.config['profile']='screen'
                for pair in s.schedule:
                    passed=pair['route']=='codex_http'
                    row=pair|{'grade':{'passed':passed},'identity_ok':True,
                              'status':'completed' if passed else failure}
                    s.db.save_outcome('screen',pair['key'],row)
                result=build_report(s)
                self.assertEqual(result['conclusion'],'incomplete_evidence',failure)
                self.assertEqual(sum(result['paired_wins'].values()),0)
    def test_adapter_exclusion_preserves_gap_without_rewriting_answer(self):
        prepare(self.cfg,self.out)
        with open_study(self.out) as s:
            for pair in s.schedule:
                s.db.save_outcome('smoke',pair['key'],pair|{'grade':{'passed':pair['route']=='sub2api'},
                                  'identity_ok':True,'status':'completed'})
            key='codex_http:smoke-json'
            s.db.set_metadata_once('quality_exclusions',{key:'unresolved adapter gap'})
            result=build_report(s)
            self.assertEqual(result['conclusion'],'incomplete_evidence')
            self.assertEqual(sum(result['paired_wins'].values()),0)
            self.assertFalse(s.db.outcome(key)['grade']['passed'])
