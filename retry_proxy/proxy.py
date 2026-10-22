"""Codex 重试代理：Codex → 本代理(127.0.0.1:50556) → Cockpit Codex API Service。

只做一件事：上游"忙"（模型满载 / 连不上 / 5xx），而且还没有任何输出时，
等一会儿用完全相同的请求重发。其余一切原样透传，Cockpit 照常负责账号。

Cockpit 的地址和密钥每次从 Codex 的 config.toml 里读（Cockpit 自己维护的
那个 provider 段），所以 Cockpit 更新、换端口、换密钥都不需要改这里。
"""
import asyncio
import json
import os
import sys
import time
import tomllib
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

HERE = Path(__file__).resolve().parent
STATE = HERE / 'state.json'
LOG = HERE / 'proxy.log'
HOST, PORT = '127.0.0.1', 50556
OWN = 'codex_retry'
DEFAULT_UPSTREAM = 'codex_local_access'
VERSION = 1

BUSY_STATUS = frozenset({500, 502, 503, 504, 529})
BUSY_CODES = frozenset({'server_is_overloaded', 'slow_down', 'overloaded'})
DELAYS = (2, 5, 10)
UNREACHABLE = 'upstream_unreachable'
PRELUDE = frozenset({'response.created', 'response.in_progress'})
HOP = frozenset({'host', 'content-length', 'connection', 'keep-alive', 'transfer-encoding', 'te',
                 'trailer', 'upgrade', 'proxy-authorization', 'proxy-connection', 'accept-encoding'})
RESP_DROP = frozenset({'content-length', 'transfer-encoding', 'connection', 'keep-alive', 'content-encoding'})


def codex_config():
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    return home / 'config.toml'


def upstream_name():
    try:
        name = json.loads(STATE.read_text(encoding='utf-8')).get('upstream')
        if isinstance(name, str) and name and name != OWN:
            return name
    except (OSError, ValueError):
        pass
    return DEFAULT_UPSTREAM


_cache = {}


def upstream(config_path=None):
    """Return (base_url, extra_headers) of the Cockpit provider Codex used before us."""
    path = Path(config_path or codex_config())
    st = path.stat()
    name = upstream_name()
    key = (str(path), st.st_mtime_ns, st.st_size, name)
    if _cache.get('key') != key:
        cfg = tomllib.loads(path.read_text(encoding='utf-8-sig'))
        provider = (cfg.get('model_providers') or {}).get(name)
        if not isinstance(provider, dict) or not provider.get('base_url'):
            raise RuntimeError(f'config.toml 里找不到 Cockpit 的 provider [{name}]，请在 Cockpit 里重新接管 Codex。')
        base = str(provider['base_url']).rstrip('/')
        if f':{PORT}' in base:
            raise RuntimeError('上游指向了代理自己，配置有误。')
        headers = {str(k): str(v) for k, v in (provider.get('http_headers') or {}).items()}
        token = provider.get('experimental_bearer_token')
        if not token and provider.get('env_key'):
            token = os.environ.get(str(provider['env_key']))
        if token:
            headers['Authorization'] = 'Bearer ' + str(token)
        _cache.clear()
        _cache.update(key=key, base=base, headers=headers)
    return _cache['base'], dict(_cache['headers'])


def busy_code(payload):
    """Overload code inside an OpenAI-style error payload / SSE event, else None."""
    if not isinstance(payload, dict):
        return None
    candidates = [payload, payload.get('error')]
    if isinstance(payload.get('response'), dict):
        candidates.append(payload['response'].get('error'))
    for item in candidates:
        if not isinstance(item, dict):
            continue
        code = item.get('code') or item.get('type')
        message = str(item.get('message') or '').lower()
        if code in BUSY_CODES:
            return code
        if 'at capacity' in message or 'overloaded' in message:
            return code if isinstance(code, str) and code and code != 'error' else 'server_is_overloaded'
    return None


def error_of(payload, status):
    """(code, message) for passing a final failure to Codex in-stream."""
    err = payload.get('error') if isinstance(payload, dict) else None
    if isinstance(err, dict):
        return str(err.get('code') or err.get('type') or f'http_{status}'), str(err.get('message') or '')
    return f'http_{status}', ''


def log(entry):
    try:
        if LOG.exists() and LOG.stat().st_size > 1_000_000:
            LOG.replace(LOG.with_suffix('.log.old'))
        with LOG.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'t': time.strftime('%Y-%m-%d %H:%M:%S')} | entry, ensure_ascii=False) + '\n')
    except OSError:
        pass


def split_blocks(buffer):
    """Split complete SSE event blocks off the front of buffer."""
    blocks = []
    while True:
        idx = buffer.find(b'\n\n')
        if idx < 0:
            return blocks, buffer
        blocks.append(bytes(buffer[:idx + 2]))
        buffer = buffer[idx + 2:]


def parse_block(block):
    """Return (event_type, data_json_or_None); (None, None) for comments/empty."""
    data, kind = [], None
    for line in block.decode('utf-8', 'replace').split('\n'):
        if line.startswith('data:'):
            data.append(line[5:].lstrip(' '))
        elif line.startswith('event:'):
            kind = line[6:].strip()
    payload = None
    if data:
        try:
            payload = json.loads('\n'.join(data))
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get('type'), str):
            kind = payload['type']
    return kind, payload


def failed_event(code, message):
    event = {'type': 'response.failed', 'response': {'object': 'response', 'status': 'failed',
             'error': {'code': code, 'message': message or code}}}
    return ('event: response.failed\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode()


def clean_response_headers(headers, keep_encoding=False):
    drop = RESP_DROP - ({'content-encoding'} if keep_encoding else set())
    return {k: v for k, v in headers.items() if k.lower() not in drop}


async def read_limited(r, limit=1 << 20):
    body = bytearray()
    async for chunk in r.aiter_bytes():
        body.extend(chunk)
        if len(body) > limit:
            break
    await r.aclose()
    return bytes(body)


def create_app(transport=None, delays=DELAYS, config_path=None):
    client = httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                               timeout=httpx.Timeout(connect=10, read=None, write=120, pool=30))
    stats = {'started': time.time(), 'requests': 0, 'retried': 0, 'recovered': 0, 'gave_up': 0}

    async def send(method, url, headers, body):
        """One attempt. Returns ('ok', response) or ('busy', code, response_or_None)."""
        try:
            r = await client.send(client.build_request(method, url, headers=headers, content=body), stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return 'busy', UNREACHABLE, None, type(exc).__name__
        if r.status_code == 200:
            return 'ok', r
        data = await read_limited(r)
        code = None
        if r.status_code in BUSY_STATUS or r.status_code == 429:
            try:
                code = busy_code(json.loads(data))
            except ValueError:
                code = None
            if code is None and r.status_code in BUSY_STATUS:
                code = f'http_{r.status_code}'
        return ('busy' if code else 'final'), code, r, data

    async def health(request):
        try:
            name = upstream_name()
            base, _ = upstream(config_path)
        except Exception as exc:  # noqa: BLE001 - reported to the operator
            name, base = upstream_name(), f'error: {exc}'
        return JSONResponse({'service': 'codex-retry-proxy', 'version': VERSION, 'pid': os.getpid(),
                             'upstream_provider': name, 'upstream_base': base, 'delays': list(delays), **stats})

    async def handle(request: Request):
        body = await request.body()
        try:
            base, extra = upstream(config_path)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({'error': {'type': 'retry_proxy_config', 'message': str(exc)}}, 502)
        path = request.url.path
        sub = path[3:] if path == '/v1' or path.startswith('/v1/') else path
        url = base + sub + (('?' + request.url.query) if request.url.query else '')
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        lowered = {k.lower(): k for k in headers}
        for k, v in extra.items():
            if k.lower() in lowered:
                headers.pop(lowered[k.lower()])
            headers[k] = v
        headers['Accept-Encoding'] = 'identity'
        retryable = request.method == 'POST' and sub.rstrip('/').endswith(('/responses', '/responses/compact'))
        if not retryable:
            try:
                r = await client.send(client.build_request(request.method, url, headers=headers, content=body), stream=True)
            except httpx.TransportError as exc:
                return JSONResponse({'error': {'type': 'retry_proxy_upstream', 'code': UNREACHABLE,
                                               'message': f'连不上 Cockpit：{type(exc).__name__}'}}, 502)
            return StreamingResponse(r.aiter_bytes(), status_code=r.status_code,
                                     headers=clean_response_headers(r.headers),
                                     background=BackgroundTask(r.aclose))
        stats['requests'] += 1
        return await retrying(request, url, headers, body, sub)

    async def retrying(request, url, headers, body, sub):
        codes = []
        attempt = 0
        # Phase A: before any HTTP headers reach Codex we can still return any status.
        while True:
            result = await send(request.method, url, headers, body)
            if result[0] == 'ok':
                r = result[1]
                break
            if result[0] == 'final':
                _, code, r, data = result
                note(sub, codes, attempt, f'http_{r.status_code}')
                return Response(data, status_code=r.status_code, headers=clean_response_headers(r.headers))
            _, code, r, data = result
            codes.append(code)
            if attempt >= len(delays):
                note(sub, codes, attempt, 'gave_up')
                if r is None:
                    return JSONResponse({'error': {'type': 'retry_proxy_upstream', 'code': UNREACHABLE,
                                        'message': f'连不上 Cockpit（{data}），已重试 {attempt} 次。'}}, 502)
                return Response(data, status_code=r.status_code, headers=clean_response_headers(r.headers))
            await asyncio.sleep(delays[attempt])
            attempt += 1

        ctype = r.headers.get('content-type', '').split(';')[0].strip().lower()
        if ctype != 'text/event-stream':
            note(sub, codes, attempt, 'ok')
            return StreamingResponse(r.aiter_bytes(), status_code=200,
                                     headers=clean_response_headers(r.headers),
                                     background=BackgroundTask(r.aclose))

        first_headers = clean_response_headers(r.headers)
        turn_state = r.headers.get('x-codex-turn-state')
        retry_headers = dict(headers)
        if turn_state and not any(k.lower() == 'x-codex-turn-state' for k in retry_headers):
            # Keep any retry consistent with the turn state Codex is about to receive.
            retry_headers['x-codex-turn-state'] = turn_state

        async def stream():
            nonlocal r, attempt
            held = []
            committed = False
            try:
                while True:
                    buffer = bytearray()
                    retry_code = None
                    async for chunk in r.aiter_bytes():
                        if committed:
                            yield chunk
                            continue
                        buffer.extend(chunk.replace(b'\r\n', b'\n'))
                        blocks, rest = split_blocks(buffer)
                        buffer = bytearray(rest)
                        for i, block in enumerate(blocks):
                            kind, payload = parse_block(block)
                            if kind is None and payload is None:
                                yield block  # comment/keepalive: carries no state
                                continue
                            if kind in PRELUDE:
                                held.append(block)
                                continue
                            if kind in ('error', 'response.failed') and attempt < len(delays):
                                retry_code = busy_code(payload)
                                if retry_code:
                                    break
                            committed = True
                            for h in held:
                                yield h
                            held.clear()
                            for b in blocks[i:]:
                                yield b
                            if buffer:
                                yield bytes(buffer)
                            buffer.clear()
                            break
                        if retry_code:
                            break
                    if not retry_code:
                        # Stream ended (or was fully committed).
                        for h in held:
                            yield h
                        if buffer:
                            yield bytes(buffer)
                        note(sub, codes, attempt, 'ok')
                        return
                    # Overloaded before any output: retry silently behind SSE comments.
                    await r.aclose()
                    codes.append(retry_code)
                    while True:
                        yield f': retry-proxy: {codes[-1]}, retrying in {delays[attempt]}s\n\n'.encode()
                        await asyncio.sleep(delays[attempt])
                        attempt += 1
                        result = await send(request.method, url, retry_headers, body)
                        if result[0] == 'ok' and result[1].headers.get('content-type', '').split(';')[0].strip().lower() == 'text/event-stream':
                            r = result[1]
                            held = []
                            break
                        if result[0] == 'ok':
                            data = await read_limited(result[1])
                            note(sub, codes, attempt, 'unexpected_non_sse')
                            yield failed_event('retry_proxy_unexpected_response', data[:500].decode('utf-8', 'replace'))
                            return
                        if result[0] == 'busy' and attempt < len(delays):
                            codes.append(result[1])
                            continue
                        _, code, resp, data = result
                        try:
                            payload = json.loads(data) if isinstance(data, (bytes, bytearray)) else {}
                        except ValueError:
                            payload = {}
                        final_code, message = error_of(payload, resp.status_code if resp else 502)
                        if result[0] == 'busy':
                            codes.append(code)
                            final_code = code if code != UNREACHABLE else final_code
                            message = message or f'上游忙，已重试 {attempt} 次仍失败。'
                        note(sub, codes, attempt, 'gave_up')
                        yield failed_event(final_code, message)
                        return
            finally:
                await r.aclose()

        return StreamingResponse(stream(), status_code=200, headers=first_headers, media_type='text/event-stream')

    def note(sub, codes, attempt, outcome):
        if codes:
            stats['retried'] += 1
            if outcome == 'ok':
                stats['recovered'] += 1
            elif outcome == 'gave_up':
                stats['gave_up'] += 1
            log({'path': sub, 'attempts': attempt + 1, 'busy': codes, 'outcome': outcome})

    routes = [Route('/__retry_proxy/health', health, methods=['GET']),
              Route('/{path:path}', handle, methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'])]
    app = Starlette(routes=routes, on_shutdown=[client.aclose])
    return app


def main():
    if sys.stdout is None or sys.stderr is None:  # pythonw: no console
        sink = open(HERE / 'stderr.log', 'a', encoding='utf-8', buffering=1)
        sys.stdout = sys.stdout or sink
        sys.stderr = sys.stderr or sink
    import uvicorn
    uvicorn.run(create_app(), host=HOST, port=PORT, log_level='warning', access_log=False)


if __name__ == '__main__':
    main()
