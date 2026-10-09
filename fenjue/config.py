"""Configuration contains references to credentials, never credential values."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """A configuration error whose message contains no credential values."""


@dataclass(frozen=True, slots=True)
class AccountConfig:
    id: str
    url: str
    api_key_env: str
    models: tuple[str, ...]
    kind: str = "responses"
    chatgpt_account_id_env: str | None = None
    max_concurrency: int = 1
    supports_output_limit: bool = True
    supported_efforts: tuple[str, ...] = ()
    credential_epoch: str = "1"
    account_binding: str = ""

    def __post_init__(self) -> None:
        if not self.account_binding:
            object.__setattr__(
                self, "account_binding", "direct" if self.kind == "codex" else "pooled"
            )


@dataclass(frozen=True, slots=True)
class Settings:
    accounts: tuple[AccountConfig, ...]
    gateway_key_env: str = "FENJUE_GATEWAY_KEY"
    host: str = "127.0.0.1"
    port: int = 8787
    timeout_seconds: float = 240
    queue_timeout_seconds: float = 15
    max_inflight: int = 16
    max_request_bytes: int = 2_097_152
    max_response_bytes: int = 8_388_608
    max_review_chars: int = 100_000
    max_output_tokens_per_call: int = 8192
    max_total_output_tokens: int = 32768
    mode: str = "pass"


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_ACCOUNT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def _string(value: object, field: str, *, limit: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > limit
        or any(ord(character) < 32 for character in value)
    ):
        raise ConfigError(f"{field} must be a nonempty string without surrounding whitespace")
    return value


def _env_name(value: object, field: str) -> str:
    name = _string(value, field, limit=128)
    if not _ENV_NAME.fullmatch(name):
        raise ConfigError(f"{field} must name an environment variable")
    return name


def _integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigError(f"{field} must be an integer from {minimum} through {maximum}")
    return value


def _duration(value: object, field: str) -> float:
    if (
        type(value) not in (int, float)
        or not 0 < value <= 3600
        or not math.isfinite(value)
    ):
        raise ConfigError(f"{field} must be a positive number no greater than 3600")
    return float(value)


def _strings(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty) or len(value) > 128:
        raise ConfigError(f"{field} must be a list of up to 128 strings")
    result = tuple(_string(item, field, limit=128) for item in value)
    if len(set(result)) != len(result):
        raise ConfigError(f"{field} contains duplicate entries")
    return result


def _endpoint(value: object, kind: str) -> str:
    url = _string(value, "account.url", limit=2048)
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ConfigError("account.url must be an absolute HTTP(S) endpoint") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in url
        or " " in url
        or not parsed.path
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ConfigError("account.url must be a full HTTP(S) endpoint without credentials or query")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ConfigError("account.url requires HTTPS except for loopback development endpoints")
    if kind == "codex" and url != "https://chatgpt.com/backend-api/codex/responses":
        raise ConfigError("codex accounts require the explicit ChatGPT Codex responses endpoint")
    return url


def _account(value: object) -> AccountConfig:
    if not isinstance(value, dict):
        raise ConfigError("Each account must be a JSON object")
    allowed = {field.name for field in fields(AccountConfig)}
    if set(value) - allowed:
        raise ConfigError("Account configuration contains an unknown field")
    if not {"id", "url", "api_key_env", "models"} <= set(value):
        raise ConfigError("Each account requires id, url, api_key_env, and models")
    account_id = _string(value["id"], "account.id", limit=64)
    if not _ACCOUNT_ID.fullmatch(account_id):
        raise ConfigError("account.id must contain letters, digits, dots, underscores, or hyphens")
    kind = value.get("kind", "responses")
    if kind not in ("responses", "codex"):
        raise ConfigError("account.kind must be responses or codex")
    binding = value.get("account_binding", "direct" if kind == "codex" else "pooled")
    if binding not in ("direct", "dedicated", "pooled"):
        raise ConfigError("account.account_binding must be direct, dedicated, or pooled")
    if (kind == "codex" and binding != "direct") or (kind == "responses" and binding == "direct"):
        raise ConfigError("codex accounts use direct binding; responses accounts use dedicated or pooled")
    account_id_env = value.get("chatgpt_account_id_env")
    if account_id_env is not None:
        account_id_env = _env_name(account_id_env, "account.chatgpt_account_id_env")
    if kind == "codex" and account_id_env is None:
        raise ConfigError("codex accounts require chatgpt_account_id_env")
    if kind != "codex" and account_id_env is not None:
        raise ConfigError("chatgpt_account_id_env applies only to codex accounts")
    supports_limit = value.get("supports_output_limit", kind != "codex")
    if type(supports_limit) is not bool:
        raise ConfigError("account.supports_output_limit must be a boolean")
    efforts = _strings(value.get("supported_efforts", []), "account.supported_efforts", allow_empty=True)
    models = _strings(value["models"], "account.models")
    if "*" in models:
        raise ConfigError("account.models must explicitly list model names")
    return AccountConfig(
        id=account_id,
        url=_endpoint(value["url"], kind),
        api_key_env=_env_name(value["api_key_env"], "account.api_key_env"),
        models=models,
        kind=kind,
        chatgpt_account_id_env=account_id_env,
        max_concurrency=_integer(value.get("max_concurrency", 1), "account.max_concurrency", 1, 128),
        supports_output_limit=supports_limit,
        supported_efforts=efforts,
        credential_epoch=_string(value.get("credential_epoch", "1"), "account.credential_epoch", limit=128),
        account_binding=binding,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("Configuration contains a duplicate JSON property")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ConfigError("Configuration contains a nonfinite JSON number")


def load_settings(path: str | Path) -> Settings:
    """Read a strict JSON configuration without accessing credential variables."""
    try:
        with Path(path).open("rb") as source:
            raw = source.read(262_145)
        if len(raw) > 262_144:
            raise ConfigError("Configuration exceeds the 256 KiB size limit")
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise ConfigError("Configuration must be a readable UTF-8 JSON file") from None
    if not isinstance(value, dict):
        raise ConfigError("Configuration must be a JSON object")
    allowed = {field.name for field in fields(Settings)}
    if set(value) - allowed:
        raise ConfigError("Configuration contains an unknown top-level field")
    accounts_raw = value.get("accounts")
    if not isinstance(accounts_raw, list) or not 1 <= len(accounts_raw) <= 128:
        raise ConfigError("accounts must contain from 1 through 128 account entries")
    accounts = tuple(_account(account) for account in accounts_raw)
    if len({account.id for account in accounts}) != len(accounts):
        raise ConfigError("Account IDs must be unique")
    defaults = Settings(accounts=accounts)
    kwargs: dict[str, object] = {"accounts": accounts}
    for name, minimum, maximum in (
        ("port", 1, 65535),
        ("max_inflight", 1, 1024),
        ("max_request_bytes", 1024, 67_108_864),
        ("max_response_bytes", 1024, 67_108_864),
        ("max_review_chars", 1024, 2_000_000),
        ("max_output_tokens_per_call", 1, 1_048_576),
        ("max_total_output_tokens", 1, 4_194_304),
    ):
        kwargs[name] = _integer(value.get(name, getattr(defaults, name)), name, minimum, maximum)
    for name in ("timeout_seconds", "queue_timeout_seconds"):
        kwargs[name] = _duration(value.get(name, getattr(defaults, name)), name)
    kwargs["gateway_key_env"] = _env_name(value.get("gateway_key_env", defaults.gateway_key_env), "gateway_key_env")
    kwargs["host"] = _string(value.get("host", defaults.host), "host", limit=253)
    mode = value.get("mode", defaults.mode)
    if mode not in ("pass", "refine"):
        raise ConfigError("mode must be pass or refine")
    kwargs["mode"] = mode
    if kwargs["max_output_tokens_per_call"] > kwargs["max_total_output_tokens"]:
        raise ConfigError("max_total_output_tokens must cover at least one full per-call budget")
    return Settings(**kwargs)
