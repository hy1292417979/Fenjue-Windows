import json
import tempfile
import unittest
from pathlib import Path
from fenjue.discovery.study import prepare, open_study

def config():
    return dict(study_id='test',profile='smoke',seed=7,max_calls=2,timeout_seconds=30,
                model='gpt-6-astra',effort='high',local_install='C:/explicit/sub2api',account_id=1,group_id=8)

class StudyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.cfg=self.root/'config.json';self.cfg.write_text(json.dumps(config()))
        self.out=self.root/'study'
    def tearDown(self):self.temp.cleanup()
    def test_freeze_and_budget(self):
        prepare(self.cfg,self.out)
        with open_study(self.out) as s:
            s.reserve('sub2api','one');s.reserve('codex_http','two')
            with self.assertRaises(Exception):s.reserve('sub2api','three')
        with open_study(self.out) as s:self.assertEqual(len(s.db.calls()),2)
    def test_tampering_rejected(self):
        prepare(self.cfg,self.out)
        p=self.out/'tasks.json';p.write_text('[]')
        with self.assertRaises(ValueError):open_study(self.out)
    def test_reprepare_preserves_existing(self):
        prepare(self.cfg,self.out)
        with self.assertRaises(FileExistsError):prepare(self.cfg,self.out)
    def test_no_secret_fields_and_budget_zero(self):
        for changes in ({'access_token':'secret'},{'max_calls':0},{'max_calls':3}):
            self.cfg.write_text(json.dumps(config()|changes))
            with self.assertRaises(ValueError):prepare(self.cfg,self.out)
