"""Frozen two-submission native-client versus direct-HTTP channel check.

The native client's own system instructions are an explicit experimental variable.
No claim of backend equivalence or quality superiority follows from this pair.
"""
import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

import httpx

from fenjue.discovery.config import read
from fenjue.discovery.local import LocalAccountSource
from fenjue.discovery.native_codex import native_codex_call
from fenjue.discovery.transport import RouteTransport, scrub
from fenjue.research.tasks import build_suite, initial_payload, grade


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--route',choices=['native','codex_http'],required=True)
    args=parser.parse_args()
    config=read('screen.proposal.json')|{'effort':'high','timeout_seconds':240}
    task=next(t for t in build_suite(config['seed'])['tasks'] if t['id']=='confirm-constraints-07')
    payload=initial_payload(task,config['model'],{'effort':'high'})
    directory=Path('runtime/native-comparison')
    directory.mkdir(parents=True,exist_ok=True)
    path=directory/(args.route+'.json')
    state={'reserved':1,'stage_maximum_client_submissions':2,'route':args.route,
           'task_id':task['id'],'status':'reserved_unknown_completion','started':time.time(),
           'task_sha256':hashlib.sha256(task['prompt'].encode()).hexdigest(),
           'requested_model':config['model'],'requested_effort':'high',
           'native_system_instructions_are_experimental_variable':True}
    source=LocalAccountSource(config)
    try:
        before=source.inventory()
        state['account_ref']=before['account_ref']
        with path.open('x',encoding='utf-8') as f:json.dump(state,f,indent=2)
        if args.route=='native':
            result=await native_codex_call(source.credentials(),payload['instructions']+'\n\n'+task['prompt'],
                                          model=config['model'],effort='high',timeout=240)
        else:
            async with httpx.AsyncClient(timeout=240,trust_env=False,follow_redirects=False) as client:
                result=await RouteTransport(source,client).call('codex_http',payload)
        state.update(scrub(result.record(),source.secrets))
        state['grade']=grade(task,result.answer) if result.ok else None
        state['binding_unchanged']=source.inventory()['account_ref']==before['account_ref']
        state['finished']=time.time()
        path.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:state.get(k) for k in ('route','status','grade','model','effort','elapsed_seconds','diagnostic','binding_unchanged')},ensure_ascii=False))
    finally:source.close()


if __name__=='__main__':asyncio.run(main())
