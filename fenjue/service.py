"""Loopback Responses service backed by the explicitly selected local account.

No hidden inference retry, prompt rewriting, tool execution, or quality claim.
Wire output stays in memory. Only metadata and bounded check scores reach disk.
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

import anyio
import httpx
import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .discovery.local import LocalAccountSource
from .discovery.transport import RouteTransport
from .sse import iter_response_events

ROUTES = ('codex_http', 'sub2api')
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')
MODEL = 'gpt-6-astra'
PORT = 17877
ORIGIN = f'http://127.0.0.1:{PORT}'
TTL = 1800
FIELDS = {'model', 'input', 'instructions', 'reasoning', 'tools', 'tool_choice',
          'parallel_tool_calls', 'text', 'include', 'stream', 'store', 'metadata'}


def binding(info):
    return {k: info[k] for k in ('account_ref', 'controls_version')}


def choose_route(report, identity, model, effort):
    if (not report or report.get('identity') != binding(identity)
            or report.get('model') != model or report.get('effort') != effort
            or not 0 <= time.time() - report.get('time', 0) < TTL):
        return 'codex_http'
    for route in ROUTES:
        if report.get('routes', {}).get(route, {}).get('passed') is True:
            return route
    raise ValueError('Recent checks failed on both routes; select a route explicitly or run new checks.')


def normalize(payload):
    if not isinstance(payload, dict) or set(payload) - FIELDS:
        raise HTTPException(400, 'Unsupported fields. Use complete input history; output caps and server-side state are not supported by this account adapter.')
    p = dict(payload)
    if p.get('model', MODEL) != MODEL:
        raise HTTPException(400, 'This installation is bound to gpt-6-astra; no model alias or fallback.')
    p['model'] = MODEL
    if not isinstance(p.get('input'), (str, list)) or not p['input']:
        raise HTTPException(400, 'input must be a nonempty string or full-history list.')
    if isinstance(p['input'], str):
        p['input'] = [{'role': 'user', 'content': p['input']}]
    if any(not isinstance(item, dict) for item in p['input']):
        raise HTTPException(400, 'input items must be objects.')
    if p.get('store') not in (None, False) or type(p.get('stream', False)) is not bool:
        raise HTTPException(400, 'Use store:false and a boolean stream flag.')
    effort = p.get('reasoning', {'effort': 'max'})
    if not isinstance(effort, dict) or effort.get('effort') not in EFFORTS or set(effort) - {'effort', 'summary'}:
        raise HTTPException(400, 'Invalid reasoning settings.')
    p['reasoning'] = effort
    if 'instructions' in p and not isinstance(p['instructions'], str):
        raise HTTPException(400, 'instructions must be a string.')
    p.setdefault('instructions', '')
    include = p.get('include', [])
    if not isinstance(include, list) or any(not isinstance(x, str) for x in include):
        raise HTTPException(400, 'include must be a list of strings.')
    p['include'] = list(dict.fromkeys(include + ['reasoning.encrypted_content']))
    p['store'], p['stream'] = False, True
    return p


class LocalBackend:
    def __init__(self, config):
        self.config = config

    async def inventory(self):
        def read():
            source = LocalAccountSource(self.config)
            try:
                return source.inventory()
            finally:
                source.close()
        return await anyio.to_thread.run_sync(read)

    async def execute(self, route, payload):
        config = self.config | {'model': payload['model'], 'effort': payload['reasoning']['effort']}
        source = LocalAccountSource(config)
        key_context = None
        entered = False
        try:
            before = await anyio.to_thread.run_sync(source.inventory)
            if route == 'sub2api':
                key_context = source.temporary_key()
                await anyio.to_thread.run_sync(key_context.__enter__)
                entered = True
            async with httpx.AsyncClient(timeout=config['timeout_seconds'], trust_env=False, follow_redirects=False) as client:
                result = await RouteTransport(source, client).call(route, payload)
            if not result.ok:
                raise RuntimeError(result.status)
            if not result.response.get('id'):
                raise RuntimeError('missing_response_id')
            after = await anyio.to_thread.run_sync(source.inventory)
            if binding(before) != binding(after):
                raise RuntimeError('account_binding_changed')
            audit = await anyio.to_thread.run_sync(source.audit, route)
            if not audit['verified']:
                raise RuntimeError('usage_identity_unverified')
            return result.response, binding(after) | {
                'elapsed_seconds': result.elapsed_seconds, 'reported_effort': result.effort,
                'wire_format': result.wire_format, 'usage_identity_verified': True}
        finally:
            # Cleanup survives client cancellation; secrets never enter logs.
            with anyio.CancelScope(shield=True):
                try:
                    if entered:
                        await anyio.to_thread.run_sync(key_context.__exit__, None, None, None)
                finally:
                    await anyio.to_thread.run_sync(source.close)


def create_app(backend, state_dir, key):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    report_path = state_dir / 'checks.json'
    slots = asyncio.Semaphore(10)
    check_lock = asyncio.Lock()
    last_check = 0

    def report():
        try:
            return json.loads(report_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    def journal(event):
        with (state_dir / 'requests.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps({'time': time.time()} | event) + '\n')

    @app.middleware('http')
    async def access(request, call_next):
        if request.headers.get('host') != f'127.0.0.1:{PORT}' or request.headers.get('origin', ORIGIN) != ORIGIN:
            return JSONResponse({'error': {'message': 'Loopback origin required.'}}, 403)
        public = request.method == 'GET' and request.url.path in ('/', '/health')
        if not public and not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + key):
            return JSONResponse({'error': {'message': 'Local API key required.'}}, 401)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(HTTPException)
    async def errors(request, exc):
        return JSONResponse({'error': {'message': str(exc.detail), 'type': 'fenjue_error'}}, exc.status_code)

    @app.get('/health')
    async def health():
        return {'service': 'fenjue', 'version': '0.2.0', 'status': 'running'}

    @app.get('/')
    async def home():
        return HTMLResponse(Path(__file__).with_name('service.html').read_text(encoding='utf-8'))

    @app.get('/v1/models')
    async def models():
        return {'object': 'list', 'data': [{'id': MODEL, 'object': 'model', 'owned_by': 'selected-account'}]}

    async def identity():
        try:
            return await backend.inventory()
        except Exception:
            raise HTTPException(503, 'Selected local account or exclusive group is unavailable.') from None

    @app.get('/api/status')
    async def status():
        info = await identity()
        return {'model': MODEL, 'default_effort': 'max', 'efforts': EFFORTS,
                'routes': ROUTES, 'account_id': info.get('account_id'), 'group_id': info.get('group_id'),
                'checks': report(), 'quality_verified': False, 'delivery': 'buffered',
                'entrypoint': ORIGIN + '/v1', 'evidence_scope': 'Local checks select a usable route; no independent full-capability baseline.'}

    async def body(request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 2_000_000:
                raise HTTPException(413, 'Request exceeds 2 MB; input is never silently truncated.')
        def pairs(values):
            d = {}
            for k, v in values:
                if k in d:
                    raise ValueError('duplicate key')
                d[k] = v
            return d
        try:
            return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
        except (ValueError, RecursionError, UnicodeError):
            raise HTTPException(400, 'Expected strict JSON without duplicate keys.') from None

    async def invoke(route, payload, info):
        request_id = secrets.token_hex(12)
        try:
            await asyncio.wait_for(slots.acquire(), 10)
        except TimeoutError:
            raise HTTPException(429, 'All 10 local execution slots are occupied.') from None
        try:
            journal({'id': request_id, 'route': route, 'model': payload['model'], 'effort': payload['reasoning']['effort'], 'status': 'submitted'})
            result, evidence = await backend.execute(route, payload)
            if binding(info) != binding(evidence):
                raise RuntimeError('account_binding_changed')
            if result.get('model') != payload['model'] or (result.get('reasoning') or {}).get('effort', payload['reasoning']['effort']) != payload['reasoning']['effort']:
                raise RuntimeError('model_or_effort_mismatch')
            journal({'id': request_id, 'status': 'completed', 'route': route,
                     'reported_effort': (result.get('reasoning') or {}).get('effort'),
                     'usage': {k: v for k, v in result.get('usage', {}).items() if k in ('input_tokens', 'output_tokens', 'total_tokens')}})
            return result, evidence
        except Exception:
            journal({'id': request_id, 'status': 'failed_or_unknown', 'route': route})
            raise HTTPException(502, 'Upstream execution or identity check failed. This request was not retried; upstream may have consumed tokens.') from None
        finally:
            slots.release()

    @app.post('/v1/responses')
    async def responses(request: Request):
        raw = await body(request)
        payload = normalize(raw)
        info = await identity()
        route = request.headers.get('x-fenjue-route', 'auto')
        if route not in (*ROUTES, 'auto'):
            raise HTTPException(400, 'Unknown route.')
        continuation = any(x.get('type') in ('reasoning', 'function_call', 'function_call_output', 'item_reference') or 'encrypted_content' in x for x in payload['input'])
        token = request.headers.get('x-fenjue-session')
        if token:
            try:
                claims = jwt.decode(token, key, algorithms=['HS256'], options={'require': ['exp', 'route', 'binding', 'model', 'effort']})
                if (claims['binding'] != binding(info) or claims['model'] != payload['model']
                        or claims['effort'] != payload['reasoning']['effort']
                        or claims['route'] not in ROUTES or route not in ('auto', claims['route'])):
                    raise ValueError()
                route = claims['route']
            except (jwt.PyJWTError, ValueError, TypeError):
                raise HTTPException(409, 'Session binding expired or changed. Start a new conversation.') from None
        elif continuation:
            raise HTTPException(409, 'Tool/reasoning continuation requires X-Fenjue-Session from the prior response.')
        if route == 'auto':
            try:
                route = choose_route(report(), info, payload['model'], payload['reasoning']['effort'])
            except ValueError as exc:
                raise HTTPException(503, str(exc)) from None
        result, evidence = await invoke(route, payload, info)
        token = jwt.encode({'binding': binding(info), 'route': route, 'model': payload['model'],
                            'effort': payload['reasoning']['effort'], 'exp': int(time.time()) + 86400}, key, algorithm='HS256')
        headers = {'X-Fenjue-Route': route, 'X-Fenjue-Session': token, 'X-Fenjue-Delivery': 'buffered',
                   'X-Fenjue-Quality-Verified': 'false',
                   'X-Fenjue-Reported-Effort': str((result.get('reasoning') or {}).get('effort') or 'unreported')}
        if raw.get('stream'):
            return StreamingResponse(iter_response_events(result), media_type='text/event-stream', headers=headers)
        return JSONResponse(result, headers=headers)

    @app.post('/api/check')
    async def check(request: Request):
        nonlocal last_check
        options = await body(request)
        if not isinstance(options, dict) or set(options) - {'effort'} or options.get('effort', 'max') not in EFFORTS:
            raise HTTPException(400, 'Only effort may be selected for checks.')
        if check_lock.locked() or time.monotonic() - last_check < 60:
            raise HTTPException(429, 'A check is active or the 60-second cooldown is active.')
        async with check_lock:
            last_check = time.monotonic()
            info = await identity()
            # Randomized known-answer checks. Three tasks share one request per route.
            # This is a functional screen, never a general intelligence benchmark.
            a, b, c = [secrets.randbelow(900) + 100 for _ in range(3)]
            nonce = secrets.token_hex(6)
            prompt = ('Return only a JSON object with keys total, order, marker. '
                      f'total is {a}*{b}+{c}. order is the unique order of A,B,C,D: '
                      'C is first; B immediately follows A; D is last. '
                      f'marker is exactly "{nonce}". No other keys.')
            expected = {'total': a*b+c, 'order': ['C', 'A', 'B', 'D'], 'marker': nonce}
            prompt += ' Represent order as an array of strings.'
            effort = options.get('effort', 'max')
            payload = normalize({'model': MODEL, 'reasoning': {'effort': effort}, 'input': prompt})
            value = {'time': time.time(), 'identity': binding(info), 'model': MODEL, 'effort': effort,
                     'calls_reserved': 2, 'scope': 'three known-answer checks; not a full-capability benchmark', 'routes': {}}
            # Reserve both calls on disk before any inference. An interruption is visible.
            report_path.write_text(json.dumps(value), encoding='utf-8')
            order = list(ROUTES)
            secrets.SystemRandom().shuffle(order)
            for route in order:
                start = time.monotonic()
                try:
                    result, _ = await invoke(route, payload, info)
                    output = result['output']
                    has_final = any(x.get('phase') == 'final_answer' for x in output)
                    text = ''.join(p.get('text', '') for x in output if x.get('type') == 'message' and (not has_final or x.get('phase') == 'final_answer') for p in x.get('content', []) if p.get('type') == 'output_text')
                    actual = json.loads(text)
                    scores = {k: type(actual.get(k)) is type(v) and actual.get(k) == v for k, v in expected.items()} if isinstance(actual, dict) else {}
                    passed = actual == expected and all(scores.values())
                    value['routes'][route] = {'passed': passed, 'checks': scores, 'status': 'completed',
                                               'reported_effort': (result.get('reasoning') or {}).get('effort')}
                except (HTTPException, ValueError, TypeError, KeyError):
                    value['routes'][route] = {'passed': False, 'status': 'execution_or_format_failure'}
                value['routes'][route]['seconds'] = round(time.monotonic() - start, 2)
                temp = report_path.with_suffix('.tmp')
                temp.write_text(json.dumps(value), encoding='utf-8')
                temp.replace(report_path)
            return value

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='screen.proposal.json')
    parser.add_argument('--state', default='runtime')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    config['timeout_seconds'] = 300
    directory = Path(args.state)
    directory.mkdir(parents=True, exist_ok=True)
    key_file = directory / 'gateway-key.local.json'
    if not key_file.exists():
        # Exclusive creation prevents competing launchers from rotating a live key.
        try:
            with key_file.open('x', encoding='utf-8') as f:
                json.dump({'api_key': 'fj-' + secrets.token_urlsafe(32)}, f)
        except FileExistsError:
            pass
    key = json.loads(key_file.read_text(encoding='utf-8'))['api_key']
    import uvicorn
    uvicorn.run(create_app(LocalBackend(config), directory, key), host='127.0.0.1', port=PORT,
                access_log=False, log_level='warning', proxy_headers=False)


if __name__ == '__main__':
    main()
