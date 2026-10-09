import json
import tempfile
import unittest
from pathlib import Path
from contextlib import contextmanager
from fenjue.discovery.study import prepare, open_study
from fenjue.discovery.runner import run, bind, continuation
from fenjue.discovery.transport import CallResult
from tests.test_discovery_study import config


class FakeSource:
    secrets=['private-secret']
    def inventory(self):return {'account_ref':'fixed','credential_version':'fixed'}
    @contextmanager
    def temporary_key(self):yield
    def audit(self, route):return {'verified':True}


class FakeTransport:
    def __init__(self, result=None):self.calls=[];self.result=result
    async def call(self, route, payload):
        self.calls.append((route,payload))
        if isinstance(self.result, BaseException):raise self.result
        return self.result or CallResult(ok=True,status='completed',answer='{"sum":391,"sorted":[2,5,9]}',model='gpt-6-astra',effort='high')


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        cfg=self.root/'config.json';cfg.write_text(json.dumps(config()))
        prepare(cfg,self.root/'study');self.study=open_study(self.root/'study')
        self.source=FakeSource();bind(self.study,self.source)
    def tearDown(self):self.study.db.close();self.tmp.cleanup()
    async def test_resume_never_replays(self):
        t=FakeTransport();await run(self.study,self.source,t);await run(self.study,self.source,t)
        self.assertEqual(len(t.calls),2);self.assertEqual(len(self.study.db.calls()),2)
        self.assertTrue(all(r['grade']['passed'] for r in self.study.db.rows('smoke')))
    async def test_reserved_interrupted_not_replayed(self):
        item=self.study.schedule[0];self.study.reserve(item['route'],item['key'])
        t=FakeTransport();await run(self.study,self.source,t)
        self.assertEqual(len(t.calls),1)
    async def test_failures_consume_budget(self):
        t=FakeTransport(CallResult(status='timeout'));await run(self.study,self.source,t)
        self.assertEqual(len(t.calls),2)
        self.assertTrue(all(not r['grade']['passed'] for r in self.study.db.rows('smoke')))
    async def test_requested_protocol_stop_retains_first_failure(self):
        t=FakeTransport(CallResult(status='not_sse'))
        result=await run(self.study,self.source,t,stop_on_protocol_error=True)
        self.assertEqual(len(t.calls),1)
        self.assertEqual(result['stopped'],'protocol_error')
        self.assertEqual(self.study.db.rows('smoke')[0]['status'],'not_sse')
    async def test_exception_reserved_and_scrubbed(self):
        t=FakeTransport(RuntimeError('private-secret'))
        await run(self.study,self.source,t)
        self.assertNotIn('private-secret',json.dumps(self.study.db.calls()))
        self.assertEqual(len(t.calls),2)
    async def test_changed_identity_stops_before_network(self):
        self.source.inventory=lambda:{'account_ref':'changed'}
        t=FakeTransport()
        with self.assertRaises(ValueError):await run(self.study,self.source,t)
        self.assertEqual(t.calls,[])
    def test_continuation_preserves_order_and_phase(self):
        task={'grader':{'kind':'tool_json','records':{'a':{'v':3}}}}
        out=[{'type':'reasoning','id':'r','summary':[],'encrypted_content':'opaque'},
             {'type':'message','role':'assistant','phase':'commentary','content':[]},
             {'type':'function_call','name':'read_record','namespace':'fixture','call_id':'c1','arguments':'{"record_id":"a"}'}]
        state={};items=continuation(task,out,state)
        self.assertEqual(items[:3],out);self.assertEqual(items[3]['call_id'],'c1')
        self.assertEqual(state['tool_trace'][0]['ok'],True)
        with self.assertRaises(ValueError):continuation(task,out,state)
        with self.assertRaises(ValueError):continuation(task,[out[-1]|{'name':'shell','call_id':'c2'}],{})
    async def test_cancel_consumes_and_does_not_retry(self):
        import asyncio
        t=FakeTransport(asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):await run(self.study,self.source,t)
        self.assertEqual(len(self.study.db.calls()),1)
        self.assertEqual(self.study.db.calls()[0]['record']['status'],'interrupted')
    async def test_multiturn_budget_and_ephemeral_reasoning(self):
        task={'id':'tools-fixture','family':'tools-fixture','category':'tools','split':'screen',
              'prompt':'Read root, then leaf; return value.', 'tools':[{'type':'function','name':'fixture.read_record'}],
              'max_turns':3,'grader':{'kind':'tool_json','expected':{'value':7},
                                    'records':{'root':{'next':'leaf'},'leaf':{'value':7}},
                                    'required_record_ids':['root','leaf']}}
        # Unit fixture injects one task into the real persisted ledger; production preparation remains frozen.
        self.study.tasks=[task];self.study.config['max_calls']=4
        self.study.db.budget={'total':4,'smoke':4}
        self.study.schedule=[{'route':r,'task_id':task['id'],'key':r+':'+task['id']} for r in ('sub2api','codex_http')]
        class ToolTransport:
            def __init__(self):self.calls=[]
            async def call(inner, route, payload):
                count=sum(r==route for r,p in inner.calls)
                import copy
                inner.calls.append((route,copy.deepcopy(payload)))
                if count<2:
                    output=[{'type':'reasoning','encrypted_content':'opaque-secret','summary':[]},
                            {'type':'function_call','name':'fixture.read_record','call_id':'call'+str(count),
                             'arguments':json.dumps({'record_id':'root' if count==0 else 'leaf'})}]
                    return CallResult(ok=True,status='completed',output=output)
                return CallResult(ok=True,status='completed',answer='{"value":7}')
        t=ToolTransport();await run(self.study,self.source,t)
        rows=self.study.db.rows('smoke')
        self.assertEqual(len(t.calls),4);self.assertTrue(rows[0]['grade']['passed'])
        self.assertEqual(rows[1]['status'],'budget_exhausted')
        self.assertIn('opaque-secret',json.dumps(t.calls[1][1]))
        self.assertNotIn('opaque-secret',json.dumps(self.study.db.calls()))
        self.assertEqual(t.calls[1][1]['input'][-1]['call_id'],'call0')
        await run(self.study,self.source,t);self.assertEqual(len(t.calls),4)
