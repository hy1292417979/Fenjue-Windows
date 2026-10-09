"""Reuse Fenjue's strict terminal/item reconciliation for native Codex traffic."""
import copy
from .discovery.transport import sse_terminal


class ProtocolError(ValueError):
    pass


def prepare(payload, default_effort='max'):
    if not isinstance(payload, dict) or not isinstance(payload.get('model'), str) or not payload['model']:
        raise ProtocolError('An explicit model is required.')
    if not isinstance(payload.get('input'), (str, list)):
        raise ProtocolError('Full input history is required.')
    if any(k in payload for k in ('previous_response_id', 'conversation')):
        raise ProtocolError('Use full input history; server-side response references are not supported.')
    if payload.get('store') not in (None, False) or payload.get('background'):
        raise ProtocolError('This stateless gateway requires store:false and foreground requests.')
    if type(payload.get('stream', False)) is not bool:
        raise ProtocolError('stream must be a boolean.')
    outgoing = copy.deepcopy(payload)
    if isinstance(outgoing['input'], str):
        outgoing['input'] = [{'role': 'user', 'content': outgoing['input']}]
    if any(not isinstance(item, dict) for item in outgoing['input']):
        raise ProtocolError('Input items must be objects.')
    reasoning = outgoing.setdefault('reasoning', {'effort': default_effort})
    if not isinstance(reasoning, dict):
        raise ProtocolError('Invalid reasoning configuration.')
    reasoning.setdefault('effort', default_effort)
    if reasoning['effort'] not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
        raise ProtocolError('Invalid reasoning effort.')
    include = outgoing.setdefault('include', [])
    if not isinstance(include, list) or any(not isinstance(v, str) for v in include):
        raise ProtocolError('include must be a string array.')
    if 'reasoning.encrypted_content' not in include:
        include.append('reasoning.encrypted_content')
    outgoing.setdefault('instructions', '')
    outgoing.update(store=False, stream=True)
    return outgoing


def expected(payload):
    # Response metadata echoes the REQUEST-level effort, including when an
    # in-history configuration_update intentionally changes effective effort.
    return {'model': payload['model'], 'reasoning': dict(payload['reasoning'])}


def effective_effort(payload):
    value = payload['reasoning']['effort']
    for item in payload['input']:
        if item.get('type') == 'configuration_update' and isinstance(item.get('reasoning'), dict):
            if 'effort' in item['reasoning']:
                value = item['reasoning']['effort']
    return value


def prepare_compact(payload, default_effort='max'):
    outgoing = prepare(payload, default_effort)
    if any(item.get('type') == 'compaction_trigger' for item in outgoing['input']):
        raise ProtocolError('Standalone compaction must not contain an existing trigger.')
    outgoing['input'].append({'type': 'compaction_trigger'})
    return outgoing


def check_response(response, payload):
    if (not isinstance(response, dict) or response.get('status') != 'completed'
            or not isinstance(response.get('id'), str) or not response['id']):
        raise ProtocolError('Upstream did not return a completed response.')
    target = expected(payload)
    if response.get('model') != target['model']:
        raise ProtocolError('Upstream model is missing or differs from the requested model.')
    reasoning = response.get('reasoning') or {}
    if not isinstance(reasoning, dict):
        raise ProtocolError('Malformed upstream reasoning metadata.')
    reported = reasoning.get('effort')
    if reported is not None and reported != target['reasoning']['effort']:
        raise ProtocolError('Upstream reasoning effort differs from the requested effort.')
    output = response.get('output')
    if not isinstance(output, list) or not output or any(not isinstance(v, dict) for v in output):
        raise ProtocolError('Missing or malformed output.')
    # Modern Codex uses custom_tool_call and additional output types. Keep all
    # items without replacing them with ordinary assistant text.
    if not any(v.get('type') != 'reasoning' for v in output):
        raise ProtocolError('Missing actionable output.')
    compaction_requested = any(i.get('type') == 'compaction_trigger' for i in payload['input'])
    compaction_present = any(i.get('type') == 'compaction' and isinstance(i.get('encrypted_content'), str)
                             and i['encrypted_content'] for i in output)
    if compaction_requested and not compaction_present:
        raise ProtocolError('Compaction trigger completed without encrypted compaction state.')
    effective = effective_effort(payload)
    return {'requested_effort': target['reasoning']['effort'], 'reported_effort': reported,
            'effective_requested_effort': effective,
            'effective_effort_verified': reported is not None and effective == target['reasoning']['effort'],
            'effort_verified': reported is not None, 'model_verified': True,
            'compaction_verified': bool(compaction_requested and compaction_present)}


def reconcile(raw, payload, state):
    # The original parser verifies ordering, conflicts and opaque item state.
    # Its older actionable-output gate recognizes only messages/functions.
    result = sse_terminal(raw, expected(payload), state)
    if result is not None and result.status == 'missing_actionable_output':
        import json
        event = json.loads(raw)
        response = event.get('response', {})
        items = state['items']
        output = response.get('output') or [items[i] for i in sorted(items)]
        response = response | {'output': [items.get(i, {}) | item for i, item in enumerate(output)]}
        check_response(response, payload)
        result.response, result.ok, result.status = response, True, 'completed'
    if result is not None:
        if not result.ok:
            raise ProtocolError(result.status)
        check_response(result.response, payload)
    return result
