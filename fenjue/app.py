"""Internal Responses gateway: authentication, affinity, and bounded delivery."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import logging
import math
import os
import re
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from typing import Any

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .config import AccountConfig, Settings
from .engine import EngineResult, run
from .sse import iter_response_events
from .upstream import UpstreamError, UpstreamPool


_LOGGER = logging.getLogger("fenjue.requests")
_SESSION_TTL = 3600
_TENANT = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def _error(code: str, message: str, status: int = 400) -> UpstreamError:
    return UpstreamError(code, message, status_code=status)


def _base64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unbase64(raw: str) -> bytes:
    return base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)


def _session_token(account: AccountConfig, model: str, tenant: str, key: bytes) -> str:
    data = {
        "v": 1, "account": account.id, "epoch": account.credential_epoch,
        "model": model, "tenant": tenant, "expires": int(time.time()) + _SESSION_TTL,
    }
    encoded = _base64(json.dumps(data, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(key, b"fenjue-affinity-v1:" + encoded.encode("ascii"), hashlib.sha256).digest()
    return encoded + "." + _base64(signature)


def _session_account(token: str, model: str, tenant: str, key: bytes, settings: Settings) -> str:
    try:
        if len(token) > 4096:
            raise ValueError("length")
        encoded, supplied = token.split(".")
        expected = hmac.new(key, b"fenjue-affinity-v1:" + encoded.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unbase64(supplied)):
            raise ValueError("signature")
        data = json.loads(_unbase64(encoded))
        if not isinstance(data, dict) or data.get("v") != 1:
            raise ValueError("version")
        if data.get("model") != model or data.get("tenant") != tenant:
            raise ValueError("scope")
        expiry = data.get("expires")
        if type(expiry) is not int or expiry < int(time.time()):
            raise ValueError("expiry")
        for account in settings.accounts:
            if account.id == data.get("account") and account.credential_epoch == data.get("epoch"):
                if account.account_binding == "pooled":
                    raise ValueError("unbound")
                return account.id
        raise ValueError("account")
    except (ValueError, TypeError, UnicodeError, KeyError):
        raise _error("invalid_session", "Session expired or changed; rebuild full context with a valid account binding.", 409) from None


def _needs_affinity(value: Any) -> bool:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, list):
            pending.extend(current)
        elif isinstance(current, dict):
            kind = current.get("type")
            if isinstance(kind, str) and (
                kind in {"item_reference", "reasoning", "compaction"}
                or kind.endswith("_call_output") or kind.endswith("_call")
            ):
                return True
            if current.get("encrypted_content"):
                return True
            pending.extend(current.values())
    return False


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON value")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON value")
    return result


async def _read_payload(request: Request, limit: int) -> dict[str, Any]:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise _error("request_too_large", "Request exceeds the configured byte limit.", 413)
    try:
        result = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, UnicodeError, RecursionError):
        raise _error("invalid_json", "Request must be a valid JSON object.") from None
    if not isinstance(result, dict):
        raise _error("invalid_request", "Request must be a JSON object.")
    if not isinstance(result.get("model"), str) or not result["model"].strip() or len(result["model"]) > 256:
        raise _error("invalid_model", "An explicit configured model is required.")
    if not isinstance(result.get("input"), (str, list)):
        raise _error("invalid_input", "Supply the complete input as text or a Responses item array.")
    if "stream" in result and type(result["stream"]) is not bool:
        raise _error("invalid_stream", "stream must be a boolean.")
    if "store" in result and type(result["store"]) is not bool:
        raise _error("invalid_store", "store must be a boolean.")
    if "background" in result and type(result["background"]) is not bool:
        raise _error("invalid_background", "background must be a boolean.")
    if result.get("background"):
        raise _error("background_unsupported", "This endpoint requires foreground requests.")
    if "previous_response_id" in result or "conversation" in result:
        raise _error("full_history_required", "Send complete history in input; server-side response references are not supported.")
    if "fenjue" in result:
        raise _error("reserved_field", "Use X-Fenjue-Mode for engine selection; fenjue is a response field.")
    return result


def _error_body(error: UpstreamError, request_id: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {"type": "fenjue_error", "code": error.code, "message": str(error)},
        "request_id": request_id,
    }
    trace = getattr(error, "trace", None)
    if isinstance(trace, dict):
        body["fenjue"] = trace
    elif error.usage is not None:
        body["fenjue"] = {"reported_usage": error.usage, "quality_verified": False}
    return body


def _decorate(result: EngineResult, request_id: str) -> dict[str, Any]:
    response = copy.deepcopy(result.response)
    trace = copy.deepcopy(result.trace)
    trace.update({"request_id": request_id, "quality_verified": False, "delivery": "buffered"})
    response["fenjue"] = trace
    return response


def _record(request_id: str, code: str, trace: dict[str, Any] | None = None) -> None:
    # Deliberately omit task content, credentials, response text, and session tokens.
    data: dict[str, Any] = {"request_id": request_id, "result": code}
    if trace:
        for name in ("call_count", "elapsed_ms", "usage_complete", "aggregate_usage", "skip_reason"):
            if name in trace:
                data[name] = trace[name]
    _LOGGER.info("%s", json.dumps(data, ensure_ascii=True, separators=(",", ":")))


async def _cancel(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await task


async def _cleanup(stack: AsyncExitStack, worker: asyncio.Task[Any] | None = None) -> None:
    # Starlette cancels response tasks on disconnect. Lease release must finish
    # even while the surrounding AnyIO cancellation scope remains cancelled.
    with anyio.CancelScope(shield=True):
        if worker is not None:
            await _cancel(worker)
        await stack.aclose()


class _LeasedStreamingResponse(StreamingResponse):
    def __init__(self, content, *, lease_stack: AsyncExitStack, **kwargs):
        super().__init__(content, **kwargs)
        self._lease_stack = lease_stack

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # This also covers send(headers) failing before the body begins.
            with anyio.CancelScope(shield=True):
                try:
                    close = getattr(self.body_iterator, "aclose", None)
                    if close is not None:
                        await close()
                finally:
                    await self._lease_stack.aclose()


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configured_key = os.environ.get(settings.gateway_key_env, "")
        if not 32 <= len(configured_key) <= 4096 or any(ord(char) < 33 or ord(char) > 126 for char in configured_key):
            raise ValueError("Gateway key must contain 32-4096 printable ASCII characters without whitespace.")
        app.state.auth_key = configured_key.encode("utf-8")
        app.state.slots = asyncio.Semaphore(settings.max_inflight)
        async with UpstreamPool(settings) as pool:
            app.state.pool = pool
            yield

    app = FastAPI(title="焚决推理引擎", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)

    def authenticate(request: Request) -> bytes:
        authorization = request.headers.get("authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        key = request.app.state.auth_key
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.encode("utf-8"), key):
            raise _error("authentication_failed", "A valid gateway bearer key is required.", 401)
        return key

    @app.exception_handler(UpstreamError)
    async def upstream_error(request: Request, error: UpstreamError):
        request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
        _record(request_id, error.code, getattr(error, "trace", None))
        return JSONResponse(_error_body(error, request_id), status_code=error.status_code, headers={"X-Request-ID": request_id})

    @app.get("/healthz")
    async def health():
        return {"status": "configured", "version": __version__, "inference_checked": False}

    @app.get("/v1/models")
    async def models(request: Request):
        authenticate(request)
        configured = sorted({model for account in settings.accounts for model in account.models})
        return {"object": "list", "data": [
            {"id": model, "object": "model", "created": 0, "owned_by": "configured-upstream"}
            for model in configured
        ], "fenjue": {"source": "operator_configuration", "availability_verified": False}}

    @app.post("/v1/responses")
    async def responses(request: Request):
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        key = authenticate(request)
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                payload = await _read_payload(request, settings.max_request_bytes)
        except TimeoutError:
            raise _error("request_body_timeout", "Request body read exceeded the configured deadline.", 408) from None
        mode = request.headers.get("x-fenjue-mode", settings.mode)
        if mode not in {"pass", "refine"}:
            raise _error("invalid_mode", "X-Fenjue-Mode must be pass or refine.")
        tenant = request.headers.get("x-fenjue-tenant", "default")
        if not _TENANT.fullmatch(tenant):
            raise _error("invalid_tenant", "Tenant identifier must use 1-128 ASCII letters, digits, dots, underscores, colons or hyphens.")
        token = request.headers.get("x-fenjue-session")
        pinned = _session_account(token, payload["model"], tenant, key, settings) if token else None
        if _needs_affinity(payload["input"]) and pinned is None:
            raise _error("session_required", "Replay the returned X-Fenjue-Session when sending reasoning or tool history.", 409)

        stack = AsyncExitStack()
        try:
            try:
                await asyncio.wait_for(request.app.state.slots.acquire(), timeout=settings.queue_timeout_seconds)
            except TimeoutError:
                raise _error("gateway_busy", "Gateway concurrency limit reached; queue wait expired.", 503) from None
            stack.callback(request.app.state.slots.release)
            session = await stack.enter_async_context(request.app.state.pool.lease(payload["model"], pinned_account=pinned))
            if session.config.account_binding == "pooled" and (payload.get("tools") or _needs_affinity(payload["input"])):
                raise _error("account_binding_required", "Tool and opaque-state workflows require a direct account or a dedicated single-account upstream.", 409)
            headers = {
                "X-Request-ID": request_id, "X-Fenjue-Mode": mode,
                "Cache-Control": "no-store", "X-Fenjue-Delivery": "buffered",
            }
            if session.config.account_binding != "pooled":
                headers["X-Fenjue-Session"] = _session_token(session.config, payload["model"], tenant, key)

            if payload.get("stream", False):
                async def stream_body():
                    worker = asyncio.create_task(run(payload, session, settings, mode=mode))
                    try:
                        yield b": fenjue preparing\n\n"
                        while not worker.done():
                            done, _ = await asyncio.wait({worker}, timeout=5)
                            if not done:
                                yield b": fenjue working\n\n"
                        result = await worker
                        # SSE construction validates before the first data frame.
                        for event in iter_response_events(_decorate(result, request_id)):
                            yield event
                        _record(request_id, "delivered", result.trace)
                    except asyncio.CancelledError:
                        _record(request_id, "client_disconnected_usage_unknown")
                        raise
                    except UpstreamError as error:
                        data = _error_body(error, request_id)
                        data.update({"type": "error", "code": error.code, "message": str(error), "sequence_number": 0})
                        yield ("event: error\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode("utf-8")
                        _record(request_id, error.code, getattr(error, "trace", None))
                    except Exception:
                        data = {"type": "error", "code": "internal_error", "message": "Internal delivery error.", "sequence_number": 0, "request_id": request_id}
                        yield ("event: error\ndata: " + json.dumps(data) + "\n\n").encode("utf-8")
                        _record(request_id, "internal_error")
                    finally:
                        await _cleanup(stack, worker)

                headers["X-Accel-Buffering"] = "no"
                return _LeasedStreamingResponse(stream_body(), lease_stack=stack, media_type="text/event-stream", headers=headers)

            worker = asyncio.create_task(run(payload, session, settings, mode=mode))
            try:
                while not worker.done():
                    await asyncio.wait({worker}, timeout=0.25)
                    if await request.is_disconnected():
                        raise _error("client_disconnected", "Client disconnected; upstream cancellation requested.", 499)
                result = await worker
                _record(request_id, "delivered", result.trace)
                return JSONResponse(_decorate(result, request_id), headers=headers)
            finally:
                await _cleanup(stack, worker)
        except BaseException:
            await _cleanup(stack)
            raise

    return app
