"""Explicit, bounded live acceptance: synthetic tools only; no user files sent."""
import argparse
import json
import secrets
import time
from pathlib import Path

import httpx

from .cockpit_guard import read_key
from .cockpit_accounts import CockpitAccounts


def run(directory, model, approve_calls, compact_only=False):
    directory = Path(directory)
    record = directory / 'acceptance.local.json'
    lock = directory / 'acceptance.lock'
    with lock.open('x'):
        pass
    try:
        state = json.loads(record.read_text()) if record.exists() else {'budget': 4, 'submissions': [], 'passed': False}
        if approve_calls != state['budget']:
            raise ValueError('approve-calls must match the persistent authorized TOTAL budget, not add a new budget.')
        if state['passed']:
            print(json.dumps({'already_passed': True, 'submissions': len(state['submissions'])}))
            return
        required = 3
        if compact_only and (state.get('tool_continuation') is not True or state.get('model') != model):
            raise ValueError('Compact-only continuation requires a recorded successful tool roundtrip for this model.')
        if state['budget'] - len(state['submissions']) < required:
            raise ValueError('Insufficient remaining budget for the acceptance sequence. No submission made.')
        def save():
            record.write_text(json.dumps(state, indent=2), encoding='utf-8')
        headers = {'Authorization': 'Bearer ' + read_key(directory / 'gateway-key.local.json'),
                   'session_id': 'fenjue-acceptance-' + secrets.token_hex(12)}
        source = CockpitAccounts(directory / 'settings.local.json')
        binding = source.snapshot().binding
        with httpx.Client(base_url='http://127.0.0.1:17878', headers=headers, timeout=310, trust_env=False) as client:
            if client.get('/health').json().get('compact_protocol') != 'responses_trigger':
                raise ValueError('A stale guard is running. Restart it before spending any acceptance budget.')
            def submit(payload, compact=False):
                if len(state['submissions']) >= state['budget']:
                    raise ValueError('Authorized total budget exhausted.')
                entry = {'time': time.time(), 'kind': 'compact' if compact else 'responses', 'status': 'submitted_unknown'}
                state['submissions'].append(entry)
                save()  # persist reservation BEFORE transport; never reset on error
                response = client.post('/v1/responses' + ('/compact' if compact else ''), json=payload)
                entry['http_status'] = response.status_code
                if response.status_code != 200:
                    entry['status'] = 'failed_or_unknown'
                    error = response.json().get('error', {})
                    if error.get('type') == 'fenjue_guard_error':
                        entry['guard_detail'] = error.get('message')
                    save()
                    raise ValueError('Acceptance upstream/gateway error; HTTP ' + str(response.status_code))
                if compact:
                    value = response.json()
                else:
                    events = [json.loads(line[5:]) for line in response.text.splitlines()
                              if line.startswith('data:') and line[5:].strip() != '[DONE]']
                    finals = [e['response'] for e in events if e.get('type') == 'response.completed']
                    if len(finals) != 1:
                        entry['status'] = 'missing_completed'
                        save()
                        raise ValueError('Acceptance response missing terminal result.')
                    value = finals[0]
                    entry.update(model=value.get('model'), effort=(value.get('reasoning') or {}).get('effort'),
                                 encrypted_reasoning_present=any(i.get('encrypted_content') for i in value['output']))
                entry['status'] = 'completed'
                save()
                return value
            if compact_only:
                marker = 'FJ_MEMORY_' + secrets.token_hex(12)
                instructions = 'Follow the synthetic test. Preserve the exact memory marker. Use requested tools; never access real files.'
                initial = [{'role': 'user', 'content': 'Remember this exact marker for later: ' + marker},
                           {'role': 'assistant', 'content': 'I will retain the marker.'}]
                result = submit({'model': model, 'instructions': instructions, 'reasoning': {'effort': 'medium'},
                                 'input': initial}, compact=True)
                if not any(i.get('type') == 'compaction' and i.get('encrypted_content') for i in result['output']):
                    raise ValueError('Encrypted compaction state missing.')
                state['compact'] = True
                save()
                request = {'model': model, 'stream': True, 'reasoning': {'effort': 'medium'}, 'instructions': instructions,
                    'input': result['output'] + [{'role': 'user', 'content': 'Call recall_marker with the exact marker I asked you to remember.'}],
                    'tools': [{'type': 'function', 'name': 'recall_marker', 'description': 'Accept the recalled marker and return a receipt.',
                        'parameters': {'type': 'object', 'properties': {'marker': {'type': 'string'}},
                                       'required': ['marker'], 'additionalProperties': False}, 'strict': True}],
                    'tool_choice': {'type': 'function', 'name': 'recall_marker'}}
                recalled = submit(request)
                calls = [i for i in recalled['output'] if i.get('type') == 'function_call' and i.get('name') == 'recall_marker']
                if len(calls) != 1 or json.loads(calls[0]['arguments']) != {'marker': marker}:
                    raise ValueError('Compacted-state memory/tool verification failed.')
                receipt = 'FJ_RECEIPT_' + secrets.token_hex(12)
                request['input'] += recalled['output'] + [{'type': 'function_call_output', 'call_id': calls[0]['call_id'], 'output': receipt},
                    {'role': 'user', 'content': 'Reply with only the receipt returned by the tool, exactly.'}]
                request['tool_choice'] = 'none'
                final = submit(request)
                answer = ''.join(c.get('text', '') for i in final['output'] if i.get('type') == 'message'
                    for c in i.get('content', []) if c.get('type') == 'output_text').strip()
                if answer != receipt:
                    raise ValueError('Post-compaction tool roundtrip failed.')
                state['compacted_tool_continuation'] = True
                save()
                if any(v.get('model') != model or (v.get('reasoning') or {}).get('effort') != 'medium' for v in (recalled, final)):
                    raise ValueError('Model/effort metadata mismatch.')
                if source.snapshot().binding != binding:
                    raise ValueError('Account binding changed during acceptance.')
                state.update(passed=True, compact=True, native_compaction_v2=True, binding=binding,
                             quality_gain_verified=False, acceptance_version=2,
                             native_compaction_evidence='Live standalone-to-native-trigger adapter with encrypted-state tool continuation; direct client v2 stream covered offline.')
                save()
                print(json.dumps({'passed': True, 'submissions': len(state['submissions']), 'compact': True,
                                  'compacted_tool_continuation': True, 'native_compaction_v2': True}))
                return
            request = {'model': model, 'stream': True, 'reasoning': {'effort': 'medium'},
                'instructions': 'Follow the synthetic test precisely. Use the named lookup tool once; then answer only the returned marker.',
                'input': [{'role': 'user', 'content': 'Look up the acceptance marker.'}],
                'tools': [{'type': 'function', 'name': 'lookup_marker', 'description': 'Return a test marker.',
                           'parameters': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}, 'strict': True}],
                'tool_choice': {'type': 'function', 'name': 'lookup_marker'}}
            first = submit(request)
            calls = [i for i in first['output'] if i.get('type') == 'function_call' and i.get('name') == 'lookup_marker']
            if len(calls) != 1:
                raise ValueError('Expected exactly one synthetic lookup call.')
            marker = 'FENJUE_' + secrets.token_hex(8)
            request['input'] += first['output'] + [{'type': 'function_call_output', 'call_id': calls[0]['call_id'], 'output': marker}]
            request['tool_choice'] = 'none'
            second = submit(request)
            answer = ''.join(c.get('text', '') for i in second['output'] if i.get('type') == 'message'
                             for c in i.get('content', []) if c.get('type') == 'output_text').strip()
            if answer != marker:
                raise ValueError('Synthetic continuation marker mismatch.')
            if any((v.get('reasoning') or {}).get('effort') != 'medium' or v.get('model') != model for v in (first, second)):
                raise ValueError('Model or effort not attested by response metadata.')
            state.update(model=model, effort='medium', tool_continuation=True, quality_gain_verified=False)
            save()
            compact = submit({'model': model, 'instructions': request['instructions'], 'input': request['input'] + second['output']}, compact=True)
            if not any(i.get('type') == 'compaction' and i.get('encrypted_content') for i in compact.get('output', [])):
                raise ValueError('Compaction state missing.')
            state.update(passed=True, binding=binding, model=model, effort='medium', tool_continuation=True, compact=True,
                         quality_gain_verified=False)
            save()
            print(json.dumps({k: state[k] for k in ('passed', 'model', 'effort', 'tool_continuation', 'compact', 'quality_gain_verified')}))
    finally:
        lock.unlink()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, default=Path('runtime/guard'))
    parser.add_argument('--model', required=True)
    parser.add_argument('--approve-calls', type=int, required=True)
    parser.add_argument('--compact-only', action='store_true')
    args = parser.parse_args()
    try:
        run(args.state, args.model, args.approve_calls, args.compact_only)
    except Exception as error:
        # Do not print request bodies or exception repr containing credentials.
        print('Acceptance failed: ' + (str(error) if isinstance(error, ValueError) else type(error).__name__))
        raise SystemExit(1)
