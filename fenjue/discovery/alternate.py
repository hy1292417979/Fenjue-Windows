"""Explicit alternative-protocol trials for the selected subscription account.

These are protocol candidates, never capability labels. No retry or silent fallback.
"""
import asyncio
import json
import time

from websockets.asyncio.client import connect as WebSocketConnect
from websockets.exceptions import InvalidStatus, WebSocketException

from .transport import CallResult, RouteTransport, sse_terminal
from ..research.routes import adapt_payload

BPS = 'https://bps.openai.com/basispoints/api/responses'
WS = 'wss://chatgpt.com/backend-api/codex/responses'


class connect(WebSocketConnect):
    """Bind each trial to its explicit endpoint; never forward credentials on redirects."""
    def process_redirect(self, exc):
        return exc


def bps_payload(payload):
    if payload.get('tools'):
        raise ValueError('This candidate supports text only; native BPS tool adaptation is separate.')
    return adapt_payload({'kind':'bps', 'models':[payload['model']],
                          'supported_efforts':[payload['reasoning']['effort']]}, payload)


async def websocket_call(url, headers, payload, timeout=180):
    started = time.monotonic()
    result = CallResult(status='missing_completed', wire_format='websocket')
    handshake = headers | {'OpenAI-Beta': 'responses_websockets=2026-02-06'}
    state = {'items': {}, 'events': {}}
    try:
        async with asyncio.timeout(timeout):
            async with connect(url, additional_headers=handshake, proxy=None, user_agent_header=None,
                               max_size=8_000_000, open_timeout=20, close_timeout=2,
                               ping_interval=None) as ws:
                await ws.send(json.dumps(payload | {'type':'response.create'}, ensure_ascii=False))
                size = 0
                async for raw in ws:
                    size += len(raw.encode('utf-8')) if isinstance(raw, str) else len(raw)
                    if size > 8_000_000:
                        raise ValueError('response size limit')
                    terminal = sse_terminal(raw, payload, state)
                    if terminal is not None:
                        result = terminal
                        break
    except InvalidStatus as exc:
        result = CallResult(status='handshake_error', http_status=exc.response.status_code)
    except TimeoutError:
        result = CallResult(status='timeout')
    except (WebSocketException, OSError):
        result = CallResult(status='connection_error')
    except (ValueError, KeyError, TypeError, AttributeError):
        result = CallResult(status='protocol_error')
    result.wire_format = 'websocket'
    result.elapsed_seconds = round(time.monotonic() - started, 3)
    return result


async def alternate_call(source, client, route, payload):
    _, headers = source.request('codex_http')
    if route == 'codex_ws':
        return await websocket_call(WS, headers, payload, source.config['timeout_seconds'])
    if route != 'bps':
        raise ValueError('Unknown alternative route')
    creds = source.credentials()
    headers = headers | {'X-Basispoints-Auth-Mode':'chatgpt',
                         'X-OpenAI-Account-Id':creds['chatgpt_account_id']}
    class BpsSource:
        config = source.config
        def request(self, ignored):
            return BPS, headers
    return await RouteTransport(BpsSource(), client).call('bps', bps_payload(payload), expected_response=payload)
