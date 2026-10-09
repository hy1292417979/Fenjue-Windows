"""Execute the explicitly selected 36-call same-account comparison, first attempts only."""
import asyncio
import hashlib
import json
from pathlib import Path

import httpx
from fenjue.discovery.study import open_study
from fenjue.discovery.local import LocalAccountSource
from fenjue.discovery.transport import RouteTransport
from fenjue.discovery.runner import bind, run
from fenjue.discovery.report import write_report
from fenjue.research.storage import write_json_new, now

STUDY = Path('studies/astra-screen-proposal-20261007')


async def execute(study, source):
    class ProgressTransport(RouteTransport):
        async def call(self, route, payload):
            count=len(study.db.calls())
            print(json.dumps({'event':'start','call':count,'route':route}),flush=True)
            result=await super().call(route,payload)
            print(json.dumps({'event':'finish','call':count,'route':route,'status':result.status,
                              'content_type':result.content_type,'wire_format':result.wire_format,
                              'seconds':result.elapsed_seconds,'diagnostic':result.diagnostic}),flush=True)
            return result
    async with httpx.AsyncClient(trust_env=False,follow_redirects=False,
                                 timeout=study.config['timeout_seconds']) as client:
        return await run(study,source,ProgressTransport(source,client),stop_on_protocol_error=True)


with open_study(STUDY) as study:
    assert study.config['max_calls']==36 and study.config['profile']=='screen'
    paths=list(Path('fenjue/discovery').glob('*.py'))+[Path('fenjue/research/tasks.py'),Path('scripts/run_screen.py')]
    hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    receipt=study.db.metadata('execution_code')
    if receipt is None:
        study.db.set_metadata_once('execution_code',hashes)
    elif receipt != hashes:
        raise SystemExit('Execution code changed; preserve this study and review before further calls.')
    preregistration = {
        'authorized_request':'要求证实；目前只有这个账号，先证实两条渠道的表现差异',
        'max_new_model_calls':36,'models':'gpt-6-astra/high','routes':['sub2api','codex_http'],
        'task_count':12,'family_count':6,'randomized_frozen_order':True,'automatic_retries':False,
        'stop_on_protocol_error':True,'quality_requires_completed_both_arms':True,
        'transport_formats':'SSE response.completed or complete application/json object=response; record actual format',
        'primary':'paired first-answer objective pass/fail, aggregate within family before directional significance',
        'secondary':'protocol completion, paired latency and token consumption',
        'significance':'two-sided exact sign test across discordant family-level mean scores; descriptive, small-sample, one window',
        'conclusion_limit':'Neither absence of observed differences nor this single account proves full model capability.'}
    path=STUDY/'preregistration.json'
    if not path.exists():write_json_new(path,preregistration)
    assert json.loads(path.read_text(encoding='utf-8'))==preregistration
    source=LocalAccountSource(study.config)
    def cleanup(record):
        study.db.connection.execute("INSERT INTO metadata(key,value) VALUES('cleanup',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(record),))
        study.db.connection.commit()
    source.on_cleanup=cleanup
    try:
        with study.db.exclusive_run():bind(study,source)
        print(json.dumps({'event':'study_start','at':now(),'previous_calls':len(study.db.calls()),'max_calls':36}),flush=True)
        result=asyncio.run(execute(study,source))
        print(json.dumps(result),flush=True)
    except (Exception,KeyboardInterrupt) as exc:
        print(json.dumps({'error':type(exc).__name__,'calls':len(study.db.calls())}),flush=True)
        raise SystemExit(1) from None
    finally:
        source.close()
        write_report(study)
