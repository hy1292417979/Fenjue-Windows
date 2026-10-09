"""Exactly four new-call slots. No rerun; no credentials or opaque items persisted."""
import json
import time
from pathlib import Path

import httpx

root = Path('runtime')
ledger = root / 'service-acceptance.json'
record = {'started': time.time(), 'maximum_calls': 4, 'reserved': 0, 'results': {}}
try:
    with ledger.open('x', encoding='utf-8') as first:
        json.dump(record, first)
except FileExistsError:
    raise SystemExit('Acceptance already reserved. Read the existing record; do not replay.') from None
key = json.loads((root / 'gateway-key.local.json').read_text())['api_key']


def save():
    ledger.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')


def reserve(count):
    if record['reserved'] + count > 4:
        raise RuntimeError('Call budget exhausted')
    record['reserved'] += count
    save()


def safe_response(response):
    return {'http': response.status_code, 'route': response.headers.get('x-fenjue-route'),
            'reported_effort': response.headers.get('x-fenjue-reported-effort')}


save()
with httpx.Client(base_url='http://127.0.0.1:17877', headers={'Authorization': 'Bearer ' + key},
                  timeout=650, trust_env=False) as client:
    reserve(2)
    response = client.post('/api/check', json={'effort': 'max'})
    record['results']['route_checks'] = response.json() if response.status_code == 200 else safe_response(response)
    save()
    print(json.dumps({'route_checks': record['results']['route_checks']}, ensure_ascii=True), flush=True)

    payload = {'model': 'gpt-6-astra', 'reasoning': {'effort': 'max'},
               'instructions': 'Use read_invoice to obtain line items. After the tool result, compute sum(quantity*unit_cents) and return only JSON with total_cents. Do not invent data.',
               'input': [{'role': 'user', 'content': 'Read invoice fixture-27 and calculate its total in cents.'}],
               'tools': [{'type': 'function', 'name': 'read_invoice', 'description': 'Read invoice line items.',
                          'parameters': {'type': 'object', 'properties': {'invoice_id': {'type': 'string'}},
                                         'required': ['invoice_id'], 'additionalProperties': False}, 'strict': True}],
               'tool_choice': {'type': 'function', 'name': 'read_invoice'}}
    reserve(1)
    response = client.post('/v1/responses', json=payload, headers={'X-Fenjue-Route': 'codex_http'})
    record['results']['tool_first'] = safe_response(response)
    save()
    if response.status_code == 200:
        body = response.json()
        calls = [x for x in body['output'] if x.get('type') == 'function_call']
        if len(calls) == 1 and calls[0].get('name') == 'read_invoice':
            call = calls[0]
            record['results']['tool_first']['requested_invoice'] = json.loads(call['arguments']).get('invoice_id') == 'fixture-27'
            tool_result = json.dumps({'line_items': [{'quantity': 7, 'unit_cents': 349}, {'quantity': 2, 'unit_cents': 1599}]})
            payload['input'] += body['output'] + [{'type': 'function_call_output', 'call_id': call['call_id'], 'output': tool_result}]
            payload['tool_choice'] = 'auto'
            reserve(1)
            response2 = client.post('/v1/responses', json=payload, headers={
                'X-Fenjue-Route': 'codex_http', 'X-Fenjue-Session': response.headers['x-fenjue-session']})
            entry = safe_response(response2)
            if response2.status_code == 200:
                output = response2.json()['output']
                finals = [x for x in output if x.get('phase') == 'final_answer']
                text = ''.join(c.get('text', '') for x in (finals or output) if x.get('type') == 'message' for c in x.get('content', []) if c.get('type') == 'output_text')
                try:
                    entry['correct'] = json.loads(text) == {'total_cents': 5641}
                except ValueError:
                    entry['correct'] = False
            record['results']['tool_continuation'] = entry
    record['finished'] = time.time()
    save()
    print(json.dumps({'reserved': record['reserved'], 'tool_first': record['results'].get('tool_first'),
                      'tool_continuation': record['results'].get('tool_continuation')}, ensure_ascii=True))
