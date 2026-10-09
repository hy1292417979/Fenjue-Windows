"""Strict Responses SSE transport; continuation items live only in memory."""
import json
import codecs
import io
import re
import time
from dataclasses import dataclass, field

import anyio
import httpx


def scrub(value, secrets=()):
    if isinstance(value, dict):
        return {k: scrub(v, secrets) for k, v in value.items()
                if k.lower() not in {'encrypted_content', 'access_token', 'refresh_token',
                                     'id_token', 'authorization', 'cookie', 'state', 'password'}}
    if isinstance(value, list):
        return [scrub(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in sorted((s for s in secrets if isinstance(s, str) and len(s) >= 4), key=len, reverse=True):
            value = value.replace(secret, '[REDACTED]')
        value = re.sub(r'(?i)Bearer\s+\S+|sk-[A-Za-z0-9_-]+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[REDACTED]', value)
        return value
    return value


@dataclass
class CallResult:
    ok: bool = False
    status: str = 'missing_completed'
    answer: str = ''
    output: list = field(default_factory=list, repr=False)
    response: dict = field(default_factory=dict, repr=False)
    model: str | None = None
    effort: str | None = None
    usage: dict = field(default_factory=dict)
    elapsed_seconds: float = 0
    http_status: int | None = None
    content_type: str | None = None
    wire_format: str | None = None
    diagnostic: dict = field(default_factory=dict)

    def record(self):
        return {k: v for k, v in vars(self).items() if k not in ('output', 'response')}


def completed(response, payload):
    result = CallResult(model=response.get('model'), effort=(response.get('reasoning') or {}).get('effort'))
    if response.get('status') != 'completed':
        result.status = 'invalid_terminal_status'
    elif result.model != payload['model']:
        result.status = 'model_mismatch_or_missing'
    elif result.effort is not None and result.effort != payload['reasoning']['effort']:
        result.status = 'effort_mismatch'
    elif not isinstance(response.get('output'), list):
        result.status = 'invalid_output'
    elif not response['output'] or not any(item.get('type') in ('message','function_call') for item in response['output']):
        result.status = 'missing_actionable_output'
    else:
        result.ok = True
        result.status = 'completed'
        result.output = response['output']
        result.response = response
        final_phase = any(item.get('phase') == 'final_answer' for item in result.output)
        result.answer = ''.join(c.get('text', '') for item in result.output
                                if item.get('type') == 'message' and item.get('role') == 'assistant'
                                and (not final_phase or item.get('phase') == 'final_answer')
                                for c in item.get('content', []) if c.get('type') == 'output_text')
        usage = response.get('usage') or {}
        result.usage = {k: usage[k] for k in ('input_tokens', 'output_tokens', 'total_tokens')
                        if type(usage.get(k)) is int}
    return result


def sse_terminal(raw, payload, state):
    if raw == '[DONE]':
        return CallResult(status='missing_completed')
    event = json.loads(raw)
    kind = event.get('type')
    tracked = kind if kind in ('response.created','response.output_item.added','response.output_item.done',
                              'response.output_text.delta','response.completed','response.failed',
                              'response.incomplete','response.function_call_arguments.done') else 'other'
    state['events'][tracked] = state['events'].get(tracked,0)+1
    if kind == 'response.output_item.done':
        index,item=event.get('output_index'),event.get('item')
        if type(index) is not int or not 0<=index<1000 or not isinstance(item,dict):
            raise ValueError('Invalid output item index')
        if index in state['items'] and state['items'][index]!=item:
            raise ValueError('Conflicting completed output items')
        state['items'][index]=item
    if kind in ('error', 'response.failed', 'response.incomplete'):
        return CallResult(status='upstream_failed')
    if kind == 'response.completed':
        response=event['response']
        items=state['items'];output_source='terminal'
        if items:
            if sorted(items)!=list(range(len(items))):
                raise ValueError('Missing output item indices')
            terminal=response.get('output')
            if not terminal:
                response=response|{'output':[items[i] for i in range(len(items))]}
                output_source='output_item_done'
            else:
                terminal = list(terminal)
                for index,item in items.items():
                    if index>=len(terminal) or any(terminal[index].get(k)!=v for k,v in item.items() if k in terminal[index]):
                        raise ValueError('Conflicting stream and terminal output')
                    # Some endpoints omit opaque continuation fields only from
                    # the terminal snapshot, even when its output is nonempty.
                    terminal[index] = item | terminal[index]
                response = response | {'output': terminal}
        result=completed(response,payload)
        result.diagnostic={'event_counts':state['events'],'item_done_count':len(items),
                           'terminal_output_count':len(event['response'].get('output') or []),
                           'output_source':output_source}
        return result
    return None


def unlabelled_sse(raw, payload):
    data = []
    state = {'items':{},'events':{}}
    for line in raw.decode('utf-8').splitlines() + ['']:
        if line == '' and data:
            terminal = sse_terminal('\n'.join(data), payload, state)
            data.clear()
            if terminal is not None:
                return terminal
        elif line.startswith('data:'):
            data.append(line[5:].lstrip(' '))
    return CallResult(status='missing_completed')


class RouteTransport:
    def __init__(self, source, client):
        self.source, self.client = source, client

    async def call(self, route, payload, expected_response=None):
        expected_response = payload if expected_response is None else expected_response
        started = time.monotonic()
        result = CallResult()
        status = None
        content_type = None
        try:
            url, headers = self.source.request(route)
            # Absolute deadline also bounds a server that keeps sending tiny events.
            with anyio.fail_after(self.source.config['timeout_seconds']):
                async with self.client.stream('POST', url, headers=headers, json=payload,
                                              follow_redirects=False) as response:
                    status = response.status_code
                    content_type = response.headers.get('content-type', '').split(';')[0].strip().lower()[:100]
                    if status != 200:
                        result.status = 'http_error'
                    elif content_type != 'text/event-stream':
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 8_000_000:
                                raise ValueError('response size limit')
                        try:
                            body = json.loads(raw)
                        except (ValueError, UnicodeError):
                            body = None
                        if (content_type == 'application/json' and isinstance(body, dict)
                                and body.get('object') == 'response'):
                            result = completed(body, expected_response)
                            result.wire_format = 'response_json'
                            result.diagnostic = {'requested_stream': True, 'returned_stream': False}
                        elif not content_type and raw.lstrip().startswith((b'data:',b'event:')):
                            result = unlabelled_sse(raw, expected_response)
                            result.wire_format = 'sse_unlabelled'
                            result.diagnostic |= {'missing_content_type': True, 'requested_stream': True}
                        else:
                            result.status = 'not_sse'
                            result.diagnostic = {'body_kind': 'json' if body is not None else
                                                 'html' if raw.lstrip().startswith(b'<') else
                                                 'sse_in_wrong_media_type' if raw.lstrip().startswith((b'data:',b'event:')) else 'other'}
                    else:
                        data, size = [], 0
                        state = {'items':{},'events':{}}

                        async def bounded_lines():
                            decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder('utf-8')(), translate=True)
                            buffer, byte_count = '', 0
                            async for chunk in response.aiter_bytes():
                                byte_count += len(chunk)
                                if byte_count > 8_000_000:
                                    raise ValueError('stream size limit')
                                buffer += decoder.decode(chunk)
                                while '\n' in buffer:
                                    line, buffer = buffer.split('\n', 1)
                                    yield line
                            buffer += decoder.decode(b'', final=True)
                            if buffer:
                                for line in buffer.splitlines():
                                    yield line

                        def dispatch():
                            if not data:
                                return None
                            raw = '\n'.join(data)
                            data.clear()
                            return sse_terminal(raw, expected_response, state)

                        async for line in bounded_lines():
                            size += len(line)
                            if size > 8_000_000:
                                raise ValueError('stream size limit')
                            if line == '':
                                terminal = dispatch()
                                if terminal is not None:
                                    result = terminal
                                    break
                            elif line.startswith('data:'):
                                data.append(line[5:].lstrip(' '))
                        else:
                            result = dispatch() or result
                        result.wire_format = 'sse'
        except TimeoutError:
            result = CallResult(status='timeout')
        except httpx.HTTPError:
            result = CallResult(status='network_error')
        except (ValueError, KeyError, TypeError, AttributeError):
            result = CallResult(status='protocol_error')
        result.elapsed_seconds = round(time.monotonic() - started, 3)
        result.http_status = status
        result.content_type = content_type
        return result
