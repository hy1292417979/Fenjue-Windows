"""Operator protocol discovery: at most six submissions, no replay or fallback."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time

import httpx
from fenjue.discovery.config import read
from fenjue.discovery.local import LocalAccountSource
from fenjue.discovery.alternate import alternate_call
from fenjue.discovery.transport import RouteTransport, scrub
from fenjue.research.tasks import build_suite, initial_payload, grade

directory = Path('runtime/alternate-trial')
directory.mkdir(parents=True, exist_ok=True)
ledger = directory / 'ledger.json'


def save(value):
    temp = ledger.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(ledger)


async def main(phase, route):
    config = read('screen.proposal.json') | {'timeout_seconds': 240, 'effort': 'max'}
    suite = build_suite(config['seed'])['tasks']
    task_ids = ['confirm-constraints-03', 'confirm-constraints-04']
    tasks = {x['id']: x for x in suite if x['id'] in task_ids}
    if not ledger.exists():
        state = {'maximum_calls': 6, 'calls': [], 'started': time.time(), 'effort': 'max',
                 'tasks': tasks, 'purpose': 'Identify compatible alternate protocols, then compare unseen tasks before Sub2API integration.',
                 'no_retries': True, 'not_full_capability_proof': True}
        with ledger.open('x', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    state = json.loads(ledger.read_text(encoding='utf-8'))
    if state['tasks'] != tasks:
        raise RuntimeError('Frozen tasks changed')
    if phase == 'probe':
        schedule = [('bps', task_ids[0]), ('codex_ws', task_ids[0]), ('codex_http', task_ids[0])]
    elif phase == 'confirm':
        if route not in ('bps', 'codex_ws'):
            raise RuntimeError('Select an alternate route')
        schedule = [('codex_http', task_ids[1]), (route, task_ids[1])]
    else:
        schedule = [('sub2api', task_ids[1])]
    source = LocalAccountSource(config)
    try:
        identity = await asyncio.to_thread(source.inventory)
        if state.get('account_ref', identity['account_ref']) != identity['account_ref']:
            raise RuntimeError('Account identity changed')
        state['account_ref'] = identity['account_ref']
        save(state)
        async with httpx.AsyncClient(timeout=240, trust_env=False, follow_redirects=False) as client:
            for channel, task_id in schedule:
                key = channel + ':' + task_id
                if any(c['key'] == key for c in state['calls']):
                    print(json.dumps({'key':key,'status':'already_submitted_skipped'}), flush=True)
                    continue
                if len(state['calls']) >= state['maximum_calls']:
                    raise RuntimeError('Six-call budget exhausted')
                task = tasks[task_id]
                payload = initial_payload(task, config['model'], {'effort':'max'})
                payload['include'] = ['reasoning.encrypted_content']
                entry = {'key':key, 'route':channel, 'task_id':task_id, 'started':time.time(),
                         'request_hash':hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                         'status':'reserved_unknown_completion'}
                state['calls'].append(entry)
                save(state)
                if channel in ('bps', 'codex_ws'):
                    result = await alternate_call(source, client, channel, payload)
                elif channel == 'sub2api':
                    with source.temporary_key():
                        result = await RouteTransport(source, client).call(channel, payload)
                        entry['audit'] = await asyncio.to_thread(source.audit, channel) if result.ok else None
                        entry['transport_receipt'] = source.sql(f"SELECT coalesce(json_agg(row_to_json(t)),'[]') FROM (SELECT id, account_id, group_id, requested_model, upstream_model, upstream_response_model, requested_reasoning_effort, reasoning_effort, openai_ws_mode FROM usage_logs WHERE api_key_id={int(source.key_id)} ORDER BY id) t")
                else:
                    result = await RouteTransport(source, client).call(channel, payload)
                entry.update(scrub(result.record(), source.secrets))
                entry['grade'] = grade(task, result.answer) if result.ok else None
                entry['finished'] = time.time()
                save(state)
                print(json.dumps({k:entry[k] for k in ('key','status','http_status','model','effort','grade','elapsed_seconds')}, ensure_ascii=False), flush=True)
    finally:
        source.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('phase', choices=['probe','confirm','sub2'])
    p.add_argument('--route', choices=['bps','codex_ws'])
    args = p.parse_args()
    lock = directory / 'run.lock'
    with lock.open('x') as f:
        f.write('active')
    try:
        asyncio.run(main(args.phase, args.route))
    finally:
        lock.unlink()
