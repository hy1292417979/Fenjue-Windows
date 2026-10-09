"""One additional end-to-end WS ingress acceptance; separate immutable reservation."""
import asyncio
import json
import time
import argparse
from pathlib import Path
from fenjue.discovery.config import read
from fenjue.discovery.local import LocalAccountSource
from fenjue.discovery.alternate import websocket_call
from fenjue.discovery.transport import scrub
from fenjue.research.tasks import build_suite, initial_payload, grade


async def main(after_ping_fix=False):
    path = Path('runtime/sub2-ws-acceptance-after-ping-fix.json' if after_ping_fix else 'runtime/sub2-ws-acceptance.json')
    state = {'reserved':1, 'maximum_calls':1, 'started':time.time(), 'status':'reserved_unknown_completion'}
    with path.open('x', encoding='utf-8') as f:
        json.dump(state, f)
    config = read('screen.proposal.json') | {'effort':'max','timeout_seconds':240}
    source = LocalAccountSource(config)
    try:
        source.inventory()
        identifier = 'confirm-constraints-06' if after_ping_fix else 'confirm-constraints-05'
        task = next(t for t in build_suite(config['seed'])['tasks'] if t['id']==identifier)
        payload = initial_payload(task, config['model'], {'effort':'max'})
        payload['include'] = ['reasoning.encrypted_content']
        state['task_id'] = task['id']
        with source.temporary_key():
            _,headers=source.request('sub2api')
            result = await websocket_call('ws://127.0.0.1:18080/v1/responses',headers,payload,timeout=240)
            state.update(scrub(result.record(),source.secrets))
            state['grade'] = grade(task,result.answer) if result.ok else None
            state['audit'] = await asyncio.to_thread(source.audit,'sub2api') if result.ok else None
            state['receipt'] = source.sql(f"SELECT coalesce(json_agg(row_to_json(t)),'[]') FROM (SELECT id,account_id,group_id,requested_model,upstream_model,upstream_response_model,requested_reasoning_effort,reasoning_effort,openai_ws_mode FROM usage_logs WHERE api_key_id={int(source.key_id)}) t")
        state['cleanup'] = source.cleanup_result
        state['finished'] = time.time()
        path.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:state.get(k) for k in ('status','grade','receipt','cleanup','elapsed_seconds')},ensure_ascii=False))
    finally:
        source.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--after-ping-fix',action='store_true')
    asyncio.run(main(parser.parse_args().after_ping_fix))
