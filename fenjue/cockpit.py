"""Local Cockpit workbench and transparent Responses bridge.

Cockpit owns accounts, routing, retries and credentials. This module reads its
sidecar configuration afresh for each request and never writes to that directory.
"""
import argparse
import asyncio
import hmac
import json
import os
import secrets
import time
from pathlib import Path

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

PORT = 17877
MAX_BODY = 8 * 1024 * 1024
DEFAULT_CONFIG = Path.home() / '.cockpit_tools/codex_local_access_sidecar/config.json'
FORWARD_HEADERS = ('session_id', 'conversation_id', 'x-codex-turn-state',
                   'x-client-request-id', 'openai-beta')


class CockpitSource:
    def __init__(self, path=DEFAULT_CONFIG):
        self.path = Path(path)

    @classmethod
    def from_config(cls, raw):
        """Return the selected loopback endpoint from one parsed config.

        Callers that also need the manifest should parse the config once and
        use this method so a Cockpit rewrite cannot mix a new key with an old
        endpoint (or vice versa).
        """
        try:
            host, port = raw['host'], raw['port']
            keys = raw['api-keys']
            if host not in ('127.0.0.1', 'localhost', '::1'):
                raise ValueError('loopback required')
            if type(port) is not int or not 1 <= port <= 65535 or port == PORT:
                raise ValueError('invalid port')
            if not isinstance(keys, list) or not keys:
                raise ValueError('no API keys')
            selected = os.environ.get('FENJUE_COCKPIT_KEY')
            if selected is None:
                if len(keys) != 1:
                    raise HTTPException(503, 'Cockpit 有多个接入密钥，请通过 FENJUE_COCKPIT_KEY 选择。')
                selected = keys[0]
            if (not isinstance(selected, str) or selected not in keys or not selected
                    or any(ord(c) < 33 or ord(c) > 126 for c in selected)):
                raise ValueError('invalid key')
            hostname = '[::1]' if host == '::1' else host
            return f'http://{hostname}:{port}', selected
        except (OSError, ValueError, KeyError, TypeError):
            raise HTTPException(503, '无法读取 Cockpit 接入配置。请先打开 Cockpit，并启用本地接入。') from None

    def connection(self):
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            raise HTTPException(503, '无法读取 Cockpit 接入配置。请先打开 Cockpit，并启用本地接入。') from None
        return self.from_config(raw)


def create_app(source, key, transport=None, timeout=300):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    origin = f'http://127.0.0.1:{PORT}'
    slots = asyncio.Semaphore(10)

    @app.middleware('http')
    async def access(request, call_next):
        if request.headers.get('host') != f'127.0.0.1:{PORT}' or request.headers.get('origin', origin) != origin:
            return JSONResponse({'error': {'message': '仅允许本机访问。'}}, 403)
        public = request.method == 'GET' and request.url.path in ('/', '/health')
        supplied = request.headers.get('authorization', '').encode('utf-8')
        if not public and not hmac.compare_digest(supplied, ('Bearer ' + key).encode('utf-8')):
            return JSONResponse({'error': {'message': '请选择本工具的连接文件。'}}, 401)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(HTTPException)
    async def error(request, exc):
        return JSONResponse({'error': {'message': exc.detail, 'type': 'fenjue_cockpit_error'}}, exc.status_code)

    @app.get('/health')
    async def health():
        return {'service': 'fenjue-cockpit', 'version': '0.1.0', 'inference_checked': False}

    @app.get('/')
    async def page():
        return HTMLResponse(Path(__file__).with_name('cockpit.html').read_text(encoding='utf-8'))

    def client():
        return httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                timeout=httpx.Timeout(timeout, connect=5))

    @app.get('/v1/models')
    async def models():
        url, upstream_key = source.connection()
        try:
            async with client() as connection:
                result = await connection.get(url + '/v1/models', headers={'Authorization': 'Bearer ' + upstream_key}, timeout=10)
            if result.status_code != 200:
                raise HTTPException(502, f'Cockpit 模型目录返回 HTTP {result.status_code}。')
            data = result.json()
            if not isinstance(data, dict) or not isinstance(data.get('data'), list):
                raise ValueError('invalid models')
            # Expose only model identifiers, never upstream account metadata.
            return {'object': 'list', 'data': [{'id': m['id'], 'object': 'model'} for m in data['data']
                    if isinstance(m, dict) and isinstance(m.get('id'), str)], 'upstream': url}
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, '无法取得 Cockpit 模型目录，请检查本地接入是否已启动。') from None

    @app.post('/v1/responses')
    async def responses(request: Request):
        # Validate the envelope, but send the original bytes and preserve every
        # caller-supplied field, tool item and opaque continuation value.
        body = bytearray()
        try:
            async with asyncio.timeout(30):
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > MAX_BODY:
                        raise HTTPException(413, '请求超过 8 MiB。')
        except TimeoutError:
            raise HTTPException(408, '读取请求超时。') from None
        try:
            def unique(pairs):
                result = {}
                for name, value in pairs:
                    if name in result:
                        raise ValueError('duplicate field')
                    result[name] = value
                return result
            def invalid(value):
                raise ValueError('nonfinite number')
            payload = json.loads(body, object_pairs_hook=unique, parse_constant=invalid)
            if (not isinstance(payload, dict) or not isinstance(payload.get('model'), str)
                    or not payload['model'].strip() or not isinstance(payload.get('input'), (str, list))
                    or type(payload.get('stream', False)) is not bool):
                raise ValueError('invalid envelope')
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, '请提供有效的 Responses 请求，包含 model 和 input。') from None

        url, upstream_key = source.connection()
        headers = {'Authorization': 'Bearer ' + upstream_key, 'Content-Type': 'application/json',
                   'Accept': 'text/event-stream, application/json', 'Accept-Encoding': 'identity'}
        for name in FORWARD_HEADERS:
            if name in request.headers:
                headers[name] = request.headers[name]
        try:
            await asyncio.wait_for(slots.acquire(), timeout=1)
        except TimeoutError:
            raise HTTPException(429, '当前请求较多，请稍后再试。') from None
        connection = client()
        upstream = None
        closed = False
        deadline = time.monotonic() + timeout

        async def close():
            nonlocal closed
            if not closed:
                closed = True
                with anyio.CancelScope(shield=True):
                    try:
                        if upstream is not None:
                            await upstream.aclose()
                        await connection.aclose()
                    finally:
                        slots.release()

        try:
            async with asyncio.timeout(timeout):
                upstream = await connection.send(connection.build_request('POST', url + '/v1/responses',
                                                    content=bytes(body), headers=headers), stream=True)
            if not 200 <= upstream.status_code < 300:
                code = upstream.status_code
                await close()
                raise HTTPException(code if 400 <= code < 600 else 502,
                                    f'Cockpit 返回 HTTP {code}。请检查所选模型、账号和额度。')
            content_type = upstream.headers.get('content-type', '').split(';')[0].strip().lower()
            outgoing = {name: upstream.headers[name] for name in FORWARD_HEADERS + ('x-request-id',)
                        if name in upstream.headers}
            outgoing['X-Fenjue-Upstream'] = 'cockpit'
            if content_type == 'text/event-stream':
                outgoing['X-Accel-Buffering'] = 'no'
                async def chunks():
                    try:
                        async with asyncio.timeout(max(0, deadline - time.monotonic())):
                            async for chunk in upstream.aiter_bytes():
                                yield chunk
                    except (httpx.HTTPError, TimeoutError):
                        yield b'event: error\ndata: {"type":"error","error":{"message":"Cockpit stream interrupted; completion unknown. No automatic retry."}}\n\n'
                    finally:
                        await close()
                return StreamingResponse(chunks(), media_type=content_type, headers=outgoing,
                                         background=BackgroundTask(close))
            if content_type != 'application/json':
                await close()
                raise HTTPException(502, 'Cockpit 返回了不支持的响应格式。')
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                content = await upstream.aread()
            await close()
            return Response(content, status_code=upstream.status_code, media_type=content_type, headers=outgoing)
        except (httpx.HTTPError, TimeoutError):
            await close()
            raise HTTPException(502, 'Cockpit 连接中断或超时；请求是否完成未知，请先检查 Cockpit 记录。') from None
        except BaseException:
            await close()
            raise

    return app


def main():
    parser = argparse.ArgumentParser(description='焚决 · Cockpit 本地工作台')
    parser.add_argument('--cockpit-config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--state', type=Path, default=Path('runtime/cockpit'))
    args = parser.parse_args()
    # Fail early before claiming to be ready, without making an inference call.
    CockpitSource(args.cockpit_config).connection()
    args.state.mkdir(parents=True, exist_ok=True)
    key_file = args.state / 'gateway-key.local.json'
    try:
        with key_file.open('x', encoding='utf-8') as target:
            json.dump({'api_key': 'fj-' + secrets.token_urlsafe(32),
                       'base_url': f'http://127.0.0.1:{PORT}/v1'}, target)
    except FileExistsError:
        pass
    key = json.loads(key_file.read_text(encoding='utf-8'))['api_key']
    if not isinstance(key, str) or len(key) < 32:
        raise ValueError('Invalid local gateway key')
    import uvicorn
    uvicorn.run(create_app(CockpitSource(args.cockpit_config), key), host='127.0.0.1', port=PORT,
                access_log=False, log_level='warning', proxy_headers=False)


if __name__ == '__main__':
    main()
