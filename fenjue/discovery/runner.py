import copy
import json

from ..research.storage import BudgetExhausted
from ..research.tasks import initial_payload, grade, execute_tool
from .transport import CallResult, scrub


def bind(study, source):
    current = source.inventory()
    previous = study.db.metadata('identity')
    if previous is None:
        study.db.set_metadata_once('identity', current)
    elif previous != current:
        raise ValueError('Selected identity or routing controls changed; create a new study')
    return current


def check_binding(study, source):
    expected = study.db.metadata('identity')
    if expected is None or source.inventory() != expected:
        raise ValueError('Missing or changed identity binding; inspect inventory for this study')


def continuation(task, output, state):
    """Replay validated output in order, then matching local fixture outputs."""
    replay, results = [], []
    seen = state.setdefault('call_ids', set())
    for item in output:
        kind = item.get('type')
        if kind == 'reasoning':
            if not isinstance(item.get('encrypted_content'), str) or not item['encrypted_content']:
                raise ValueError('Missing reasoning continuation content')
            replay.append({k: item[k] for k in ('type', 'id', 'summary', 'encrypted_content') if k in item})
        elif kind == 'message':
            if item.get('role') != 'assistant':
                raise ValueError('Invalid continuation role')
            replay.append({k: item[k] for k in ('type', 'id', 'role', 'phase', 'content', 'status') if k in item})
        elif kind == 'function_call':
            call_id = item.get('call_id')
            if not isinstance(call_id, str) or not call_id or call_id in seen or len(seen) >= 16:
                raise ValueError('Missing, duplicate or excessive tool call identifiers')
            seen.add(call_id)
            name = item.get('name')
            namespace = item.get('namespace')
            if name == 'read_record' and namespace == 'fixture':
                name = 'fixture.read_record'
            args = json.loads(item['arguments'])
            result = execute_tool(task, name, args, state)
            if not result['ok']:
                raise ValueError('Tool call outside frozen fixture contract')
            replay.append({k: item[k] for k in ('type', 'id', 'name', 'namespace', 'arguments', 'call_id', 'status') if k in item})
            results.append({'type': 'function_call_output', 'call_id': call_id,
                            'output': json.dumps(result, ensure_ascii=False)})
        else:
            raise ValueError('Unsupported continuation item')
    if not results:
        raise ValueError('No continuation tool calls')
    return replay + results


async def run(study, source, transport, *, stop_on_protocol_error=False):
    config = study.config
    tasks = {t['id']: t for t in study.tasks}
    with study.db.exclusive_run():
        check_binding(study, source)
        with source.temporary_key():
            for pair in study.schedule:
                route, key = pair['route'], pair['key']
                if study.db.has_attempt(key) or study.db.outcome(key):
                    continue
                task = tasks[pair['task_id']]
                payload = initial_payload(task, config['model'], {'effort': config['effort']})
                payload['tools'] = copy.deepcopy(task['tools'])
                payload['include'] = ['reasoning.encrypted_content']
                state, records = {}, []
                result = CallResult(status='not_submitted')
                identity_ok = True
                for turn in range(task['max_turns']):
                    check_binding(study, source)
                    try:
                        call_id = study.reserve(route, key)
                    except BudgetExhausted:
                        result = CallResult(status='budget_exhausted')
                        break
                    try:
                        result = await transport.call(route, payload)
                    except Exception:
                        result = CallResult(status='client_error')
                    except BaseException:
                        study.db.finish_call(call_id, {'status': 'interrupted', 'ok': False})
                        raise
                    audit = {'verified': False, 'basis': 'audit_not_completed'}
                    try:
                        check_binding(study, source)
                        audit = source.audit(route)
                        identity_ok = audit['verified'] is True
                    except Exception:
                        identity_ok = False
                    record = scrub(result.record() | {'audit': audit, 'turn': turn + 1}, source.secrets)
                    study.db.finish_call(call_id, record)
                    records.append(record)
                    if not result.ok or not identity_ok:
                        break
                    if not any(item.get('type') == 'function_call' for item in result.output):
                        break
                    if turn + 1 >= task['max_turns']:
                        result = CallResult(status='tool_turn_limit')
                        break
                    try:
                        payload['input'].extend(continuation(task, result.output, state))
                    except (ValueError, KeyError, TypeError):
                        result = CallResult(status='invalid_tool_continuation')
                        break
                score = grade(task, result.answer, state.get('tool_trace', [])) if result.ok and identity_ok else {
                    'passed': False, 'score': 0.0, 'reason': result.status if identity_ok else 'identity_gap'}
                outcome = {'key': key, 'route': route, 'task_id': task['id'], 'family': task['family'],
                           'category': task['category'], 'status': result.status, 'identity_ok': identity_ok,
                           'grade': score, 'calls': len(records), 'answer': result.answer,
                           'elapsed_seconds': round(sum(r['elapsed_seconds'] for r in records), 3)}
                study.db.save_outcome(config['profile'], key, scrub(outcome, source.secrets))
                if not identity_ok:
                    return {'stopped': 'identity_gap', 'calls': len(study.db.calls())}
                if stop_on_protocol_error and result.status != 'completed':
                    return {'stopped': 'protocol_error', 'calls': len(study.db.calls())}
    return {'stopped': None, 'calls': len(study.db.calls())}
