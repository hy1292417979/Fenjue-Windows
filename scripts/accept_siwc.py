"""One SIWC submission, sharing the frozen native/HTTP constraint task."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time

import anyio
import httpx
import jwt

from fenjue.discovery.config import read
from fenjue.discovery.local import LocalAccountSource
from fenjue.discovery.siwc import SiwcSource
from fenjue.discovery.transport import scrub
from fenjue.research.tasks import build_suite, initial_payload, grade
from fenjue.siwc_login import SecureStore


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['inventory', 'accept'], default='inventory')
    args = parser.parse_args()
    config = read('screen.proposal.json')
    source = LocalAccountSource(config)
    try:
        identity = source.inventory()
        claims = jwt.decode(source.credentials()['access_token'], options={'verify_signature': False})
        expected = hashlib.sha256(claims['sub'].encode()).hexdigest()
        saved = SecureStore(Path('runtime/siwc')).read('local.json')
        siwc = SiwcSource(saved or {}, expected)
        async with httpx.AsyncClient(timeout=240, trust_env=False, follow_redirects=False) as client:
            with anyio.fail_after(30):
                models = await siwc.catalog(client)
            print(json.dumps({'route': 'siwc', 'catalog_verified': True,
                              'requested_model_available': config['model'] in models,
                              'models': models}), flush=True)
            if args.mode == 'inventory':
                return
            if config['model'] not in models:
                raise ValueError('Requested model is absent from SIWC catalog')
            task = next(t for t in build_suite(config['seed'])['tasks'] if t['id'] == 'confirm-constraints-07')
            payload = initial_payload(task, config['model'], {'effort': 'high'})
            payload = {k: v for k, v in payload.items() if k in ('model', 'input', 'instructions', 'reasoning')}
            payload.update(store=False, stream=True)
            directory = Path('runtime/siwc-trial')
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / 'acceptance.json'
            state = {'reserved': 1, 'stage_maximum_client_submissions': 1, 'route': 'siwc',
                     'status': 'reserved_unknown_completion', 'started': time.time(),
                     'task_id': task['id'], 'account_ref': identity['account_ref'],
                     'task_sha256': hashlib.sha256(task['prompt'].encode()).hexdigest(),
                     'requested_model': config['model'], 'requested_effort': 'high'}
            with path.open('x', encoding='utf-8') as output:
                json.dump(state, output, indent=2)
            result = await siwc.call(client, payload, models)
            state.update(scrub(result.record(), [saved['access_token']]))
            state['grade'] = grade(task, result.answer) if result.ok else None
            state['binding_unchanged'] = source.inventory()['account_ref'] == identity['account_ref']
            state['finished'] = time.time()
            path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({k: state.get(k) for k in ('route', 'status', 'model', 'effort', 'grade', 'elapsed_seconds')}))
    finally:
        source.close()


if __name__ == '__main__':
    asyncio.run(main())
