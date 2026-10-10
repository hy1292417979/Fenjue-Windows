"""Codex gateway implementing Fenjue's original route safeguards.

Cockpit manages login/refresh and selects the OAuth identity for each outbound
request. Direct guard ingress retains the legacy pinned route for compatibility.
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .cockpit_accounts import CockpitAccounts, BindingError, DIRECT, DEFAULT_CONFIG, Snapshot, fingerprint
from .guard_protocol import ProtocolError, prepare, prepare_compact, expected, check_response, reconcile
from .service import choose_route
from .sse import iter_response_events

PORT = 17878
MAX_BYTES = 32 * 1024 * 1024
STATE = Path('runtime/guard')


def strict_json(raw):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError('duplicate key')
            result[k] = v
        return result
    def reject(value):
        raise ValueError('nonfinite')
    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('nonfinite')
        return number
    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject, parse_float=finite)


class Ledger:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'guard.sqlite'
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions (sid TEXT PRIMARY KEY, binding TEXT, route TEXT, updated REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, started REAL, route TEXT, model TEXT, effort TEXT, status TEXT, metadata TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS checks (id TEXT PRIMARY KEY, created REAL, report TEXT)')

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def bind(self, sid, binding, route):
        # No prompts, reasoning, tokens, raw session IDs or tool output in SQLite.
        sid = hashlib.sha256(sid.encode()).hexdigest()
        binding = json.dumps(binding, sort_keys=True)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT binding,route FROM sessions WHERE sid=?', (sid,)).fetchone()
            if previous and previous != (binding, route):
                raise BindingError('Session account or route changed; start a new Codex task instead of replaying old state.')
            db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)', (sid, binding, route, time.time()))

    def start(self, route, payload):
        rid = secrets.token_hex(12)
        with self.db() as db:
            db.execute('INSERT INTO calls VALUES (?,?,?,?,?,?,?)',
                       (rid, time.time(), route, payload['model'], expected(payload)['reasoning']['effort'], 'submitted', '{}'))
        return rid

    def finish(self, rid, status, metadata):
        with self.db() as db:
            db.execute('UPDATE calls SET status=?,metadata=? WHERE id=?', (status, json.dumps(metadata), rid))

    def latest_check(self):
        with self.db() as db:
            row = db.execute('SELECT report FROM checks ORDER BY created DESC LIMIT 1').fetchone()
        return json.loads(row[0]) if row else None

    def save_check(self, rid, report):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO checks VALUES (?,?,?)', (rid, report['time'], json.dumps(report)))


class Backend:
    def __init__(self, source, transport=None):
        self.source, self.transport = source, transport

    def client(self, snapshot, route):
        return httpx.AsyncClient(proxy=snapshot.proxy if route == 'codex_http' and self.transport is None else None,
                                 transport=self.transport, trust_env=False, follow_redirects=False,
                                 timeout=httpx.Timeout(300, connect=15))

    async def catalog(self, snapshot):
        # Use this exact OAuth account's catalog, not Cockpit's combined pool.
        async with self.client(snapshot, 'codex_http') as client:
            r = await client.get(DIRECT + '/models', params={'client_version': '0.160.1'},
                                 headers=snapshot.headers('codex_http', {}), timeout=20)
        if r.status_code != 200:
            raise ProtocolError(f'Account model catalog returned HTTP {r.status_code}.')
        data = r.json()
        items = data.get('models', data.get('data', []))
        if not isinstance(items, list):
            raise ProtocolError('Invalid account model catalog.')
        return [{'id': m.get('slug', m.get('id')), 'object': 'model'} for m in items
                if isinstance(m, dict) and isinstance(m.get('slug', m.get('id')), str)]

    async def cockpit_catalog(self):
        # Cockpit owns a dynamic pool's model catalog; never pick an arbitrary
        # OAuth account merely to answer a non-generating discovery request.
        local_url, local_key = self.source.files()[-2:]
        async with httpx.AsyncClient(transport=self.transport, trust_env=False,
                                     follow_redirects=False, timeout=20) as client:
            result = await client.get(local_url + '/v1/models',
                                      headers={'Authorization': 'Bearer ' + local_key})
        if result.status_code != 200:
            raise ProtocolError(f'Cockpit model catalog returned HTTP {result.status_code}.')
        value = strict_json(result.content)
        if not isinstance(value, dict) or not isinstance(value.get('data'), list):
            raise ProtocolError('Invalid Cockpit model catalog.')
        return [{'id': item['id'], 'object': 'model'} for item in value['data']
                if isinstance(item, dict) and isinstance(item.get('id'), str)]

    async def execute(self, route, payload, incoming, snapshot, compact=False, on_headers=None):
        endpoint = (DIRECT if route == 'codex_http' else snapshot.local_url + '/v1') + '/responses'
        native_compact = compact and route == 'codex_http'
        if native_compact:
            # Native GPT-6 compaction uses the Responses trigger protocol.
            # One submission, no 404 retry/fallback, no hand-written summary.
            payload = prepare_compact(payload, self.source.settings().get('default_effort', 'max'))
        elif compact:
            endpoint += '/compact'
        limit = self.source.settings().get('timeout_seconds', 300)
        started = time.monotonic()
        raw = bytearray()
        with anyio.fail_after(limit):
            async with self.client(snapshot, route) as client:
                async with client.stream('POST', endpoint, json=payload, headers=snapshot.headers(route, incoming)) as r:
                    if r.status_code != 200:
                        raise ProtocolError(f'Upstream HTTP {r.status_code}; no account, model or route fallback was attempted.')
                    response_headers = {name: r.headers[name] for name in ('x-codex-turn-state', 'x-request-id') if name in r.headers}
                    if on_headers:
                        on_headers(response_headers)
                    content_type = r.headers.get('content-type', '').split(';')[0]
                    async for chunk in r.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_BYTES:
                            raise ProtocolError('Upstream response exceeded 32 MiB; response was not truncated.')
        if incoming.get('x-fenjue-ingress') == 'cockpit':
            selected = getattr(self.source, 'selected_snapshot', None)
            current = (selected(incoming.get('x-fenjue-oauth', ''),
                                incoming.get('x-fenjue-account', ''),
                                incoming.get('x-fenjue-auth-id', ''))
                       if selected is not None else self.source.snapshot())
        else:
            current = self.source.snapshot()
        if current.binding != snapshot.binding:
            raise BindingError('Account binding changed while the response was in flight.')
        if compact and not native_compact:
            response = strict_json(raw)
            if (not isinstance(response, dict) or response.get('object') != 'response.compaction'
                    or not isinstance(response.get('output'), list)
                    or any(not isinstance(item, dict) for item in response['output'])
                    or not any(item.get('type') == 'compaction' and item.get('encrypted_content') for item in response['output'])):
                shape = {'object': response.get('object'), 'output_types': [i.get('type') for i in response.get('output', []) if isinstance(i, dict)]} if isinstance(response, dict) else {'json_object': False}
                raise ProtocolError('Incomplete compaction result; opaque compaction state is required. Shape: ' + json.dumps(shape))
            if response.get('model', payload['model']) != payload['model']:
                raise ProtocolError('Compaction model mismatch.')
            return response, [], response_headers, {'compaction_verified': True}
        frames, response = [], None
        if content_type == 'text/event-stream' or (not content_type and raw.lstrip().startswith((b'data:', b'event:'))):
            state = {'items': {}, 'events': {}}
            block = []
            for line in raw.decode('utf-8-sig').splitlines() + ['']:
                if line:
                    block.append(line)
                    continue
                if not block:
                    continue
                data = '\n'.join(s[5:].lstrip(' ') for s in block if s.startswith('data:'))
                terminal = reconcile(data, payload, state) if data else None
                if terminal is not None:
                    response = terminal.response
                    event = strict_json(data)
                    event['response'] = response
                    # Preserve original Codex event types and deltas. Only repair
                    # the terminal snapshot from authoritative item.done events.
                    block = [s for s in block if not s.startswith('data:')]
                    block.append('data: ' + json.dumps(event, ensure_ascii=True))
                frames.append(('\n'.join(block) + '\n\n').encode())
                block = []
                if terminal is not None:
                    break
        elif content_type == 'application/json':
            response = strict_json(raw)
        else:
            raise ProtocolError('Unsupported upstream response encoding.')
        evidence = check_response(response, payload)
        evidence['elapsed_seconds'] = round(time.monotonic() - started, 3)
        evidence['output_items'] = len(response['output'])
        if native_compact:
            compacted = {key: response[key] for key in ('id', 'created_at', 'output', 'usage') if key in response}
            compacted['object'] = 'response.compaction'
            evidence['compaction_protocol'] = 'responses_trigger'
            return compacted, [], response_headers, evidence
        if not frames:
            frames = list(iter_response_events(response))
        return response, frames, response_headers, evidence


def create_app(source, local_key, directory=STATE, transport=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    ledger = Ledger(Path(directory))
    backend = Backend(source, transport)
    slots = asyncio.Semaphore(10)
    check_lock = asyncio.Lock()
    origin = f'http://127.0.0.1:{PORT}'

    @app.exception_handler(HTTPException)
    async def error(request, exc):
        return JSONResponse({'error': {'type': 'fenjue_guard_error', 'message': exc.detail}}, exc.status_code)

    @app.middleware('http')
    async def authenticate(request, call_next):
        if (request.headers.get('host') not in (f'127.0.0.1:{PORT}', f'localhost:{PORT}')
                or request.headers.get('origin', origin) not in (origin, f'http://localhost:{PORT}')):
            return JSONResponse({'error': {'message': 'Loopback access only.'}}, 403)
        if request.method == 'GET' and request.url.path in ('/', '/health'):
            return await call_next(request)
        accepted = [local_key]
        try:
            if not request.url.path.startswith('/cockpit/'):
                accepted.append(source.accepted_local_key())
        except Exception:
            pass
        supplied = request.headers.get('authorization', '').encode()
        if not any(hmac.compare_digest(supplied, ('Bearer ' + k).encode()) for k in accepted):
            return JSONResponse({'error': {'message': 'Local API key required.'}}, 401)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/health')
    async def health():
        return {'service': 'fenjue-cockpit-guard', 'version': '3.1', 'build': 'fenjue-outbound-2', 'cockpit_outbound_supported': True,
                'compact_protocol': 'responses_trigger', 'quality_gain_verified': False}

    @app.get('/')
    async def home():
        return HTMLResponse('<meta charset="utf-8"><title>焚决 · Cockpit 出站保护</title><h1>焚决 v3 · Cockpit 出站保护</h1>'
            '<p>服务在线不等于接管已开启。请双击项目目录的 Status-Guard.cmd 查看实际状态。</p>'
            '<p>新链路：Codex → Cockpit 当前选中的 GPT OAuth 账号 → 焚决 → 官方上游。</p>'
            '<p>双击焚决.cmd 开启；Stop-Guard.cmd 关闭并恢复 Cockpit 原出站。Codex 不应再设置到 17878。</p>'
            '<p>保护仅覆盖选定接入密钥的 GPT/Codex HTTP Responses 及压缩请求；不是聊天网页。质量提升尚未验证。</p>')

    def snapshot():
        try:
            return source.snapshot()
        except Exception:
            raise HTTPException(503, 'Cockpit OAuth account is unavailable or its configuration changed; inspect local guard settings.') from None

    def cockpit_snapshot(request):
        token = request.headers.get('x-fenjue-oauth', '')
        account = request.headers.get('x-fenjue-account', '')
        auth_id = request.headers.get('x-fenjue-auth-id', '')
        if not token or not account or not auth_id:
            raise HTTPException(409, 'Cockpit did not provide the selected OAuth credential.')
        try:
            selected = getattr(source, 'selected_snapshot', None)
            if selected is not None:
                return selected(token, account, auth_id)
            # Compatibility for older test/source adapters that only expose a
            # pinned snapshot. Production Cockpit sources use selected_snapshot.
            info = snapshot()
            if (not hmac.compare_digest(token.encode(), info.token.encode())
                    or account != info.account_id
                    or auth_id.lower() != source.settings().get('auth_file', '').lower()):
                raise BindingError('Cockpit selected a different or stale OAuth credential; no substitution allowed.')
            return replace(info, token=token, account_id=account)
        except BindingError as exc:
            status = 403 if 'outside' in str(exc).lower() or 'scope' in str(exc).lower() else 409
            raise HTTPException(status, str(exc)) from None

    @app.post('/cockpit/probe')
    async def cockpit_probe(request: Request):
        info = cockpit_snapshot(request)
        return {'probe': 'cockpit-selected-oauth-to-fenjue', 'account_ref': info.account_ref[:12],
                'generation_submitted': False, 'identity_matched': True}

    @app.get('/api/status')
    async def status():
        dynamic = False
        try:
            settings = source.settings()
            dynamic = settings.get('mode') == 'dynamic'
            if dynamic:
                _, manifest, scope, accounts, url, key = source.files()
                controls = fingerprint([
                    scope.get('accountIds', []),
                    scope.get('allowedModels'),
                    scope.get('excludedModels'),
                    scope.get('modelRouting'),
                    [a.get('id') for a in accounts],
                ])
                info = Snapshot('dynamic', controls, '', '', None, url, key, False)
            else:
                info = snapshot()
        except BindingError as exc:
            raise HTTPException(503, str(exc)) from None
        try:
            acceptance = json.loads((Path(directory) / 'acceptance.local.json').read_text())
            acceptance = {k: acceptance.get(k, False) for k in ('passed', 'tool_continuation', 'compact')}
        except (OSError, ValueError):
            acceptance = {'passed': False}
        with ledger.db() as db:
            last = db.execute("SELECT started,route,model,effort,status FROM calls WHERE route='cockpit_outbound' ORDER BY started DESC LIMIT 1").fetchone()
        return {'service': 'fenjue-cockpit-guard', 'account_ref': info.account_ref[:12],
                'last_call': dict(zip(('started', 'ingress_route', 'model', 'effort', 'status'), last)) if last else None,
                'outbound_acceptance': {'live_generation_verified': False},
                'default_route': 'codex_http',
                'cockpit_same_account_comparison_available': False if dynamic else info.cockpit_exclusive,
                'checks': ledger.latest_check(), 'quality_gain_verified': False, 'acceptance': acceptance,
                'upstream_identity_basis': 'selected Cockpit OAuth credential, not independent attestation',
                'delivery': 'validated_buffered_sse', 'default_effort_when_omitted': source.settings().get('default_effort', 'max')}

    @app.get('/v1/models')
    async def models():
        try:
            dynamic = source.settings().get('mode') == 'dynamic'
            info = None if dynamic else snapshot()
            data = await backend.cockpit_catalog() if dynamic else await backend.catalog(info)
            validator = getattr(source, 'validate_model', None)
            if validator:
                visible = []
                for item in data:
                    try:
                        validator(item['id'], info)
                    except BindingError:
                        continue
                    visible.append(item)
                data = visible
            return {'object': 'list', 'data': data}
        except (ProtocolError, ValueError, httpx.HTTPError, TimeoutError):
            raise HTTPException(502, 'Cockpit model catalog is unavailable.') from None

    async def body(request):
        raw = bytearray()
        try:
            with anyio.fail_after(30):
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        raise HTTPException(413, 'Request exceeded 32 MiB; history was not truncated.')
            return strict_json(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, 'Expected strict JSON without duplicate keys.') from None
        except TimeoutError:
            raise HTTPException(408, 'Request body timeout.') from None

    def select_route(info, explicit='auto'):
        if explicit not in ('auto', 'codex_http', 'cockpit'):
            raise HTTPException(400, 'Unknown route.')
        if explicit == 'cockpit' and not info.cockpit_exclusive:
            raise HTTPException(409, 'Cockpit key covers multiple accounts; same-account routing cannot be guaranteed.')
        return explicit

    async def execute(route, payload, headers, info, compact=False, on_headers=None):
        try:
            await asyncio.wait_for(slots.acquire(), 10)
        except TimeoutError:
            raise HTTPException(429, 'All execution slots are occupied.') from None
        rid = None
        try:
            rid = ledger.start('cockpit_outbound' if headers.get('x-fenjue-ingress') == 'cockpit' else route,
                               prepare(payload) if compact else payload)
            result = await backend.execute(route, payload, headers, info, compact, on_headers)
            ledger.finish(rid, 'completed', result[3])
            return result
        except asyncio.CancelledError:
            if rid:
                ledger.finish(rid, 'cancelled_completion_unknown', {})
            raise
        except Exception as exc:
            if rid:
                ledger.finish(rid, 'failed_or_unknown', {'error_type': type(exc).__name__,
                    'detail': str(exc) if isinstance(exc, (ProtocolError, BindingError)) else 'not_logged'})
            detail = str(exc) if isinstance(exc, (ProtocolError, BindingError)) else 'Upstream connection/protocol failure; completion and usage may be unknown.'
            raise HTTPException(502, detail) from None
        finally:
            slots.release()

    async def handle(request, compact=False):
        original = await body(request)
        try:
            prepared = prepare(original, source.settings().get('default_effort', 'max'))
        except (ProtocolError, ValueError, TypeError):
            raise HTTPException(400, 'Unsupported request; use full Responses history with an explicit model and valid reasoning effort.') from None
        outbound = request.url.path.startswith('/cockpit/')
        # For Cockpit ingress, bind this request to the identity Cockpit chose
        # for this call. Direct guard ingress retains the legacy pinned route.
        info = cockpit_snapshot(request) if outbound else snapshot()
        validator = getattr(source, 'validate_model', None)
        if validator:
            try:
                validator(prepared['model'], info)
            except BindingError:
                raise HTTPException(409, 'The requested model is rejected by Cockpit policy.') from None
        route = 'codex_http' if outbound else select_route(info, request.headers.get('x-fenjue-route', 'auto'))
        if route == 'auto':
            report = ledger.latest_check()
            # Reuse the original preference/TTL logic, translating the host name.
            if report:
                report = report | {'routes': {'codex_http': report['routes'].get('codex_http', {}),
                                              'sub2api': report['routes'].get('cockpit', {})}}
            try:
                route = choose_route(report, info.binding, prepared['model'], expected(prepared)['reasoning']['effort'])
            except ValueError as exc:
                raise HTTPException(503, str(exc)) from None
            route = 'cockpit' if route == 'sub2api' else route
            select_route(info, route)
        # Cockpit/native Codex has emitted both legacy underscore and modern
        # hyphenated session names.  Bind either spelling before forwarding.
        sid = (request.headers.get('session_id') or request.headers.get('session-id')
               or request.headers.get('conversation_id') or prepared.get('prompt_cache_key'))
        continuation = any(item.get('type') not in (None, 'message') for item in prepared['input'])
        if not isinstance(sid, str) or not sid or len(sid) > 512:
            if continuation:
                raise HTTPException(409, 'Codex continuation requires session_id, conversation_id or prompt_cache_key.')
            sid = secrets.token_hex(16)
        try:
            ledger.bind(sid, info.binding, 'cockpit_outbound' if outbound else route)
        except BindingError as exc:
            raise HTTPException(409, str(exc)) from None
        headers = dict(request.headers)
        headers.pop('x-fenjue-ingress', None)
        if outbound:
            headers['x-fenjue-ingress'] = 'cockpit'
        headers.setdefault('session_id', sid)
        if compact:
            # Compact has its own schema. Never send response-only defaults.
            prepared = dict(original)
            prepared.setdefault('instructions', '')
            result, _, outgoing, evidence = await execute(route, prepared, headers, info, compact=True)
            return JSONResponse(result, headers=outgoing | {'X-Fenjue-Route': route})
        if not original.get('stream', False):
            task = asyncio.create_task(execute(route, prepared, headers, info))
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=.25)
                    if await request.is_disconnected():
                        raise HTTPException(499, 'Client disconnected; completion unknown.')
                result, _, outgoing, evidence = await task
                return JSONResponse(result, headers=outgoing | {'X-Fenjue-Route': route,
                    'X-Fenjue-Effort-Verified': str(evidence['effort_verified']).lower()})
            finally:
                if not task.done():
                    task.cancel()
                    with anyio.CancelScope(shield=True):
                        await asyncio.gather(task, return_exceptions=True)

        # Wait only for upstream headers; keep turn-state headers and still send
        # heartbeats while buffering/validating the response body.
        upstream_headers = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(execute(route, prepared, headers, info,
                    on_headers=lambda value: upstream_headers.set_result(value)))
        try:
            while not upstream_headers.done() and not task.done():
                await asyncio.wait({task, upstream_headers}, timeout=.25, return_when=asyncio.FIRST_COMPLETED)
                if await request.is_disconnected():
                    raise HTTPException(499, 'Client disconnected; completion unknown.')
            if task.done():
                await task  # propagate connection/status failures before HTTP 200
            outgoing = upstream_headers.result()
        except BaseException:
            task.cancel()
            with anyio.CancelScope(shield=True):
                await asyncio.gather(task, return_exceptions=True)
            raise

        async def stream():
            try:
                yield b': fenjue validating upstream\n\n'
                while not task.done():
                    await asyncio.wait({task}, timeout=5)
                    if not task.done():
                        yield b': fenjue waiting\n\n'
                _, frames, _, _ = await task
                for frame in frames:
                    yield frame
            except HTTPException as exc:
                yield ('event: error\ndata: ' + json.dumps({'type': 'error', 'error': {'code': 'fenjue_guard_rejected', 'message': exc.detail}}) + '\n\n').encode()
            finally:
                if not task.done():
                    task.cancel()
                with anyio.CancelScope(shield=True):
                    await asyncio.gather(task, return_exceptions=True)
        return StreamingResponse(stream(), media_type='text/event-stream',
             headers=outgoing | {'X-Fenjue-Route': route, 'X-Fenjue-Delivery': 'validated-buffered', 'X-Accel-Buffering': 'no',
                      'X-Fenjue-Quality-Verified': 'false'})

    @app.post('/v1/responses')
    async def responses(request: Request):
        return await handle(request)

    @app.post('/v1/responses/compact')
    async def compact(request: Request):
        return await handle(request, compact=True)

    @app.post('/cockpit/v1/responses')
    async def cockpit_responses(request: Request):
        return await handle(request)

    @app.post('/cockpit/v1/responses/compact')
    async def cockpit_compact(request: Request):
        return await handle(request, compact=True)

    @app.post('/api/check')
    async def check(request: Request):
        options = await body(request)
        if (not isinstance(options, dict) or options.get('approve_calls') != 2
                or not isinstance(options.get('model'), str)):
            raise HTTPException(400, 'Provide model, effort and approve_calls:2 for two real generations.')
        info = snapshot()
        if not info.cockpit_exclusive:
            raise HTTPException(409, 'Two-route comparison needs a Cockpit key restricted to the pinned OAuth account. No generation submitted.')
        if check_lock.locked():
            raise HTTPException(429, 'A comparison is already running.')
        async with check_lock:
            old = ledger.latest_check()
            if old and time.time() - old['time'] < 60:
                raise HTTPException(429, 'Comparison cooldown is 60 seconds.')
            number = secrets.randbelow(900) + 100
            marker = secrets.token_hex(6)
            answer = {'total': number * number + number, 'order': ['C', 'A', 'B', 'D'], 'marker': marker}
            prompt = (f'Return only JSON: total={number}*{number}+{number}; order is an array of A,B,C,D '
                      f'with C first, B immediately after A and D last; marker="{marker}". No other keys.')
            try:
                payload = prepare({'model': options['model'], 'input': prompt,
                                   'reasoning': {'effort': options.get('effort', 'max')}})
            except ProtocolError as exc:
                raise HTTPException(400, str(exc)) from None
            rid = secrets.token_hex(12)
            value = {'time': time.time(), 'identity': info.binding, 'model': payload['model'],
                     'effort': payload['reasoning']['effort'], 'calls_reserved': 2, 'routes': {},
                     'scope': 'known-answer functional screen, not proof of full model quality'}
            ledger.save_check(rid, value)
            routes = ['codex_http', 'cockpit']
            secrets.SystemRandom().shuffle(routes)
            for route in routes:
                try:
                    result, _, _, evidence = await execute(route, payload, {'session_id': secrets.token_hex(16)}, info)
                    items = [i for i in result['output'] if i.get('type') == 'message']
                    finals = [i for i in items if i.get('phase') == 'final_answer']
                    text = ''.join(c.get('text', '') for i in (finals or items) for c in i.get('content', []) if c.get('type') == 'output_text')
                    actual = strict_json(text)
                    passed = actual == answer and type(actual.get('total')) is int
                    value['routes'][route] = {'passed': passed, 'status': 'completed', **evidence}
                except (HTTPException, ValueError, TypeError, KeyError):
                    value['routes'][route] = {'passed': False, 'status': 'execution_or_format_failure'}
                ledger.save_check(rid, value)
            return value

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, default=STATE)
    parser.add_argument('--cockpit-config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--account')
    parser.add_argument('--initialize', action='store_true')
    args = parser.parse_args()
    args.state.mkdir(parents=True, exist_ok=True)
    source = CockpitAccounts(args.state / 'settings.local.json', args.cockpit_config)
    binding = source.initialize(args.account)
    key_file = args.state / 'gateway-key.local.json'
    try:
        with key_file.open('x', encoding='utf-8') as target:
            json.dump({'api_key': 'fj-' + secrets.token_urlsafe(32), 'base_url': f'http://127.0.0.1:{PORT}/v1'}, target)
    except FileExistsError:
        pass
    if args.initialize:
        print(json.dumps({'initialized': True, 'account_ref': binding['account_ref'][:12]}))
        return
    key = read_key(key_file)
    import uvicorn
    uvicorn.run(create_app(source, key, args.state), host='127.0.0.1', port=PORT, access_log=False,
                log_level='warning', proxy_headers=False)


def read_key(path):
    key = json.loads(path.read_text(encoding='utf-8'))['api_key']
    if not isinstance(key, str) or len(key) < 32:
        raise ValueError('Invalid local guard key.')
    return key


if __name__ == '__main__':
    main()
