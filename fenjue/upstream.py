"""Bounded Responses transport with an account lease for the whole intervention."""

from __future__ import annotations

import asyncio
import codecs
import json
import math
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import AsyncIterator

import httpx

from .config import AccountConfig, ConfigError, Settings


class UpstreamError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 502, usage: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.usage = usage


def _usage(value: object) -> dict | None:
    """Keep recognized numeric accounting only; error bodies never become logs."""
    if not isinstance(value, dict):
        return None
    result: dict = {}
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        count = value.get(name)
        if type(count) is int and count >= 0:
            result[name] = count
    for name, key in (("input_tokens_details", "cached_tokens"), ("output_tokens_details", "reasoning_tokens")):
        details = value.get(name)
        if isinstance(details, dict) and type(details.get(key)) is int and details[key] >= 0:
            result[name] = {key: details[key]}
    return result or None


def _credential(name: str) -> str:
    value = os.environ.get(name)
    if not value or value != value.strip() or len(value) > 32768 or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ConfigError(f"Required credential environment variable {name} is missing or malformed")
    return value


def _reject_constant(_: str) -> None:
    raise ValueError("Nonfinite JSON number")


def _finite_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("Nonfinite JSON number")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _json_object(raw: str | bytes) -> dict:
    try:
        value = json.loads(raw, parse_constant=_reject_constant, parse_float=_finite_float, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise UpstreamError("invalid_upstream_json", "Upstream returned malformed JSON") from None
    if not isinstance(value, dict):
        raise UpstreamError("invalid_upstream_json", "Upstream JSON must be an object")
    return value


def _terminal(value: dict, requested_model: str) -> dict:
    usage = _usage(value.get("usage"))
    status = value.get("status")
    if status not in ("completed", "failed", "incomplete"):
        raise UpstreamError("missing_terminal_status", "Upstream did not return a terminal Responses status", usage=usage)
    if status == "failed":
        raise UpstreamError("upstream_failed", "Upstream reported a failed response", usage=usage)
    if not isinstance(value.get("output"), list):
        raise UpstreamError("invalid_upstream_output", "Upstream terminal response is missing its output items", usage=usage)
    if any(not isinstance(item, dict) for item in value["output"]):
        raise UpstreamError("invalid_upstream_output", "Upstream output items must be objects", usage=usage)
    if not isinstance(value.get("id"), str) or not value["id"]:
        raise UpstreamError("invalid_upstream_id", "Upstream terminal response is missing its response identifier", usage=usage)
    reported_model = value.get("model")
    if not isinstance(reported_model, str) or not reported_model:
        raise UpstreamError("missing_upstream_model", "Upstream terminal response is missing its model", usage=usage)
    if reported_model != requested_model:
        raise UpstreamError("upstream_model_mismatch", "Upstream reported a different model; no model alias was assumed", usage=usage)
    return value


async def _sse_objects(response: httpx.Response, max_bytes: int) -> AsyncIterator[dict]:
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    buffered = ""
    data_lines: list[str] = []
    seen_bytes = 0
    events = 0
    max_events = 100_000
    skip_lf = False

    def event() -> dict | None:
        nonlocal events
        events += 1
        if events > max_events:
            raise UpstreamError("upstream_event_limit", "Upstream stream exceeded its event limit")
        if not data_lines:
            return None
        data = "\n".join(data_lines)
        data_lines.clear()
        if data.strip() == "[DONE]":
            raise UpstreamError("missing_terminal_status", "Upstream stream ended before a terminal response")
        return _json_object(data)

    try:
        async for chunk in response.aiter_bytes():
            seen_bytes += len(chunk)
            if seen_bytes > max_bytes:
                raise UpstreamError("upstream_response_limit", "Upstream response exceeded its byte limit")
            text = decoder.decode(chunk)
            # SSE permits LF, CRLF, or CR. Preserve the CRLF boundary across chunks.
            if skip_lf and text:
                text = text.removeprefix("\n")
                skip_lf = False
            if text.endswith("\r"):
                skip_lf = True
            buffered += text.replace("\r\n", "\n").replace("\r", "\n")
            lines = buffered.split("\n")
            buffered = lines.pop()
            for line in lines:
                if not line:
                    value = event()
                    if value is not None:
                        yield value
                elif line.startswith("data:"):
                    data_lines.append(line[5:].removeprefix(" "))
        buffered += decoder.decode(b"", final=True)
    except UnicodeError:
        raise UpstreamError("invalid_upstream_encoding", "Upstream stream is not valid UTF-8") from None
    if buffered:
        line = buffered.removesuffix("\r")
        if line.startswith("data:"):
            data_lines.append(line[5:].removeprefix(" "))
    if data_lines:
        value = event()
        if value is not None:
            yield value


class _NoCookies(DefaultCookiePolicy):
    """Credentials come from configuration; response cookies carry across no calls."""

    def set_ok(self, cookie, request) -> bool:
        return False

    def return_ok(self, cookie, request) -> bool:
        return False


@dataclass(slots=True)
class UpstreamAccount:
    config: AccountConfig
    semaphore: asyncio.Semaphore = field(init=False, repr=False)
    _headers: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.semaphore = asyncio.Semaphore(self.config.max_concurrency)


class AccountSession:
    def __init__(self, account: UpstreamAccount, pool: UpstreamPool):
        self.config = account.config
        self._account = account
        self._pool = pool
        self._active = True
        self._calling = False

    async def call(self, payload: dict, timeout: float) -> dict:
        """Make exactly one fresh HTTP request; uncertain results are never retried."""
        if not self._active or self._pool._client is None:
            raise UpstreamError("expired_account_lease", "The upstream account lease is no longer active", 503)
        if self._calling:
            raise UpstreamError("concurrent_session_call", "Calls within an account lease must be sequential", 409)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise UpstreamError("request_deadline", "The request deadline has elapsed", 504)
        model = payload.get("model")
        if not isinstance(model, str) or model not in self.config.models:
            raise UpstreamError("unsupported_model", "The leased account does not support the requested model", 400)
        if "max_output_tokens" in payload and not self.config.supports_output_limit:
            raise UpstreamError("output_limit_unsupported", "This account does not support an upstream output-token limit", 400)
        if self.config.kind == "codex" and payload.get("store") is True:
            raise UpstreamError("storage_unsupported", "This Codex route requires store=false; the storage request was preserved as an error", 400)
        reasoning = payload.get("reasoning")
        effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
        if effort is not None and self.config.supported_efforts and effort not in self.config.supported_efforts:
            raise UpstreamError("unsupported_effort", "The leased account does not support the requested reasoning effort", 400)
        outgoing = dict(payload)
        outgoing["stream"] = True
        if self.config.kind == "codex":
            outgoing["store"] = False
            outgoing.setdefault("instructions", "")
            if isinstance(outgoing.get("input"), str):
                outgoing["input"] = [
                    {"role": "user", "content": [{"type": "input_text", "text": outgoing["input"]}]}
                ]
        try:
            encoded = json.dumps(outgoing, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise UpstreamError("invalid_request_json", "The upstream request must contain valid JSON values", 400) from None
        if len(encoded) > self._pool.settings.max_request_bytes:
            raise UpstreamError("upstream_request_limit", "The assembled upstream request exceeds its byte limit", 413)
        self._calling = True
        try:
            async with asyncio.timeout(timeout):
                async with self._pool._client.stream(
                    "POST", self.config.url, content=encoded,
                    headers=self._account._headers, timeout=httpx.Timeout(timeout),
                ) as response:
                    if not 200 <= response.status_code < 300:
                        status = response.status_code
                        if status in (401, 403):
                            raise UpstreamError("upstream_authentication", "Upstream rejected the configured account credentials", 502)
                        if status == 429:
                            raise UpstreamError("upstream_rate_limited", "Upstream rate limited the account", 429)
                        raise UpstreamError("upstream_http_error", f"Upstream returned HTTP {status}", 502)
                    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                        raise UpstreamError("unsupported_upstream_encoding", "Upstream ignored the requested identity content encoding")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type == "text/event-stream":
                        async for value in _sse_objects(response, self._pool.settings.max_response_bytes):
                            event_type = value.get("type")
                            if event_type == "error":
                                raise UpstreamError("upstream_stream_error", "Upstream reported a stream error", usage=_usage(value.get("usage")))
                            if event_type in ("response.completed", "response.failed", "response.incomplete", "response.done"):
                                terminal = value.get("response")
                                if not isinstance(terminal, dict):
                                    raise UpstreamError("invalid_upstream_terminal", "Upstream terminal event is missing its response")
                                if event_type != "response.done" and terminal.get("status") != event_type.removeprefix("response."):
                                    raise UpstreamError("invalid_upstream_terminal", "Upstream terminal event conflicts with its response status", usage=_usage(terminal.get("usage")))
                                return _terminal(terminal, model)
                        raise UpstreamError("missing_terminal_status", "Upstream stream closed before a terminal response")
                    if content_type != "application/json" and not content_type.endswith("+json"):
                        raise UpstreamError("unexpected_upstream_content", "Upstream returned an unsupported response content type")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > self._pool.settings.max_response_bytes:
                            raise UpstreamError("upstream_response_limit", "Upstream response exceeded its byte limit")
                        body.extend(chunk)
                    return _terminal(_json_object(bytes(body)), model)
        except (TimeoutError, httpx.TimeoutException):
            raise UpstreamError("upstream_timeout", "Upstream timed out; execution and token usage may be unknown", 504) from None
        except httpx.HTTPError:
            raise UpstreamError("upstream_connection", "Upstream connection failed; execution and token usage may be unknown") from None
        finally:
            self._calling = False


class UpstreamPool:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.accounts = tuple(UpstreamAccount(config) for config in settings.accounts)
        self._client: httpx.AsyncClient | None = None
        self._condition = asyncio.Condition()
        self._cursor = 0

    async def __aenter__(self) -> UpstreamPool:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def start(self) -> None:
        if self._client is not None:
            return
        # Validate all names before creating the client. These headers never enter repr/logs.
        headers: list[dict[str, str]] = []
        for account in self.accounts:
            entry = {
                "Authorization": f"Bearer {_credential(account.config.api_key_env)}",
                "Accept": "text/event-stream, application/json",
                "Accept-Encoding": "identity",
                "Content-Type": "application/json",
            }
            if account.config.chatgpt_account_id_env:
                entry["ChatGPT-Account-Id"] = _credential(account.config.chatgpt_account_id_env)
            headers.append(entry)
        self._client = httpx.AsyncClient(
            follow_redirects=False, trust_env=False,
            cookies=CookieJar(policy=_NoCookies()),
            limits=httpx.Limits(max_connections=self.settings.max_inflight, max_keepalive_connections=self.settings.max_inflight),
        )
        for account, entry in zip(self.accounts, headers):
            account._headers = entry

    async def aclose(self) -> None:
        async with self._condition:
            client, self._client = self._client, None
            self._condition.notify_all()
        try:
            if client is not None:
                await client.aclose()
        finally:
            for account in self.accounts:
                account._headers.clear()

    async def close(self) -> None:
        await self.aclose()

    def account_summaries(self) -> list[dict]:
        """Configuration/availability metadata; never expose credentials or upstream headers."""
        return [
            {
                "id": account.config.id,
                "kind": account.config.kind,
                "models": list(account.config.models),
                "account_binding": account.config.account_binding,
                "max_concurrency": account.config.max_concurrency,
                "supports_output_limit": account.config.supports_output_limit,
                "supported_efforts": list(account.config.supported_efforts),
                "credential_epoch": account.config.credential_epoch,
                "credentials_loaded": bool(account._headers),
                "available_capacity": not account.semaphore.locked(),
            }
            for account in self.accounts
        ]

    @asynccontextmanager
    async def lease(self, model: str, pinned_account: str | None = None) -> AsyncIterator[AccountSession]:
        eligible = [account for account in self.accounts if model in account.config.models and (pinned_account is None or account.config.id == pinned_account)]
        if not eligible:
            code = "account_affinity_unavailable" if pinned_account is not None else "unsupported_model"
            raise UpstreamError(code, "No configured account matches the requested model and account binding", 503 if pinned_account else 400)
        deadline = time.monotonic() + self.settings.queue_timeout_seconds
        async with self._condition:
            while True:
                if self._client is None:
                    raise UpstreamError("upstream_pool_closed", "The upstream account pool is not running", 503)
                selected = None
                for offset in range(len(eligible)):
                    index = (self._cursor + offset) % len(eligible)
                    candidate = eligible[index]
                    if not candidate.semaphore.locked():
                        await candidate.semaphore.acquire()
                        selected = candidate
                        self._cursor = index + 1
                        break
                if selected is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UpstreamError("account_queue_timeout", "Timed out waiting for upstream account capacity", 503)
                try:
                    await asyncio.wait_for(self._condition.wait(), remaining)
                except TimeoutError:
                    raise UpstreamError("account_queue_timeout", "Timed out waiting for upstream account capacity", 503) from None
        session = AccountSession(selected, self)
        try:
            yield session
        finally:
            session._active = False
            async with self._condition:
                selected.semaphore.release()
                self._condition.notify_all()
