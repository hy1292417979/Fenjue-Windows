"""Independent, one-request research adapters for three explicitly selected routes.

No local credential discovery, refresh, retries, proxy inheritance, redirects,
or response-cookie reuse. Importing this module and adapting a payload are offline.
Only ``preflight`` fetches identity keys/catalogs; only ``call`` does inference.
SIWC contracts: developers.openai.com/siwc/token-sharing-open-source/{
models-and-inference,preview-limitations,token-reference} (2026-10-07).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import urlencode, urlsplit
from uuid import uuid4


ENDPOINTS = {
    "codex": "https://chatgpt.com/backend-api/codex/responses",
    "siwc": "https://api.openai.com/v1/responses",
    "bps": "https://bps.openai.com/basispoints/api/responses",
}
CATALOGS = {
    "codex": "https://chatgpt.com/backend-api/codex/models",
    "siwc": "https://api.openai.com/v1/models",
}
_ISSUER = "https://auth.openai.com"
_AUDIENCE = "https://api.openai.com/v1"
_AUTH_CLAIM = "https://api.openai.com/auth"
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,127}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z")
_FIELDS = {
    "model", "input", "instructions", "reasoning", "tools", "tool_choice",
    "parallel_tool_calls", "include", "text", "store", "stream",
}
_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


class RouteConfigError(ValueError):
    """A local error whose code/message never interpolate supplied values."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _RouteFailure(Exception):
    def __init__(self, code: str, *, error_type: str | None = None, http_status: int | None = None):
        self.code = code
        self.error_type = error_type
        self.http_status = http_status
        super().__init__(code)


def _encoded(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise RouteConfigError("invalid_request_json") from None


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _hash_identity(kind: str, value: str) -> str:
    return hashlib.sha256(("fenjue-research-v1:" + kind + ":" + value).encode("utf-8")).hexdigest()


def _same_identity(actual: str, expected: str) -> bool:
    return hmac.compare_digest(actual.encode("utf-8"), expected.encode("utf-8"))


def _unknown(value: dict, allowed: set[str], code: str) -> None:
    if set(value) - allowed:
        raise RouteConfigError(code)


def _validate_tools(tools: object, kind: str, *, nested: bool = False) -> None:
    if not isinstance(tools, list) or len(tools) > 128:
        raise RouteConfigError("invalid_tools")
    for tool in tools:
        if not isinstance(tool, dict):
            raise RouteConfigError("invalid_tools")
        tool_type = tool.get("type")
        if not isinstance(tool_type, str):
            raise RouteConfigError("invalid_tool_type")
        if tool_type == "namespace" and not nested:
            _unknown(tool, {"type", "name", "description", "tools"}, "unknown_tool_field")
            if not isinstance(tool.get("name"), str) or not tool["name"]:
                raise RouteConfigError("invalid_tool_name")
            _validate_tools(tool.get("tools"), kind, nested=True)
        elif tool_type in {"function", "custom"}:
            if kind == "siwc" and not nested:
                raise RouteConfigError("siwc_tools_require_namespace")
            allowed = {"type", "name", "description", "parameters", "strict"} if tool_type == "function" else {
                "type", "name", "description", "format",
            }
            _unknown(tool, allowed, "unknown_tool_field")
            if not isinstance(tool.get("name"), str) or not tool["name"]:
                raise RouteConfigError("invalid_tool_name")
            if tool_type == "function" and not isinstance(tool.get("parameters"), dict):
                raise RouteConfigError("invalid_tool_parameters")
            if "strict" in tool and type(tool["strict"]) is not bool:
                raise RouteConfigError("invalid_tool_strict")
        elif tool_type == "web_search" and not nested:
            _unknown(tool, {"type", "filters", "search_context_size", "user_location", "external_web_access"},
                     "unknown_tool_field")
        else:
            raise RouteConfigError("unsupported_tool_type")


def _validate_input(items: object, kind: str, *, bps_tools: bool = False) -> None:
    if not isinstance(items, list) or not items:
        raise RouteConfigError("input_must_be_nonempty_array")
    for item in items:
        if not isinstance(item, dict):
            raise RouteConfigError("invalid_input_item")
        item_type = item.get("type", "message")
        if not isinstance(item_type, str):
            raise RouteConfigError("invalid_input_type")
        if item_type == "message":
            _unknown(item, {"type", "role", "content", "id", "status", "phase"}, "unknown_input_field")
            if item.get("role") not in ("user", "assistant", "developer"):
                raise RouteConfigError("unsupported_message_role")
            if "phase" in item and item["phase"] not in ("commentary", "final_answer"):
                raise RouteConfigError("unsupported_message_phase")
            content = item.get("content")
            if isinstance(content, str):
                continue
            if not isinstance(content, list) or not content:
                raise RouteConfigError("invalid_message_content")
            for part in content:
                if not isinstance(part, dict):
                    raise RouteConfigError("invalid_content_part")
                part_type = part.get("type")
                if not isinstance(part_type, str):
                    raise RouteConfigError("invalid_content_type")
                if part_type in {"input_text", "output_text"}:
                    _unknown(part, {"type", "text", "annotations", "logprobs"}, "unknown_content_field")
                    if not isinstance(part.get("text"), str):
                        raise RouteConfigError("invalid_text_content")
                elif part_type == "refusal" and item.get("role") == "assistant":
                    _unknown(part, {"type", "refusal"}, "unknown_content_field")
                    if not isinstance(part.get("refusal"), str):
                        raise RouteConfigError("invalid_text_content")
                else:
                    # This experiment's minimal adapter is deliberately text-only.
                    raise RouteConfigError("unsupported_input_modality")
        elif (kind != "bps" or bps_tools) and item_type in {"function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"}:
            allowed = {"type", "id", "call_id", "name", "namespace", "arguments", "input", "output", "status"}
            _unknown(item, allowed, "unknown_input_field")
            if not isinstance(item.get("call_id"), str) or not item["call_id"]:
                raise RouteConfigError("missing_tool_call_id")
            content_key = {"function_call": "arguments", "function_call_output": "output",
                           "custom_tool_call": "input", "custom_tool_call_output": "output"}[item_type]
            if not isinstance(item.get(content_key), str):
                raise RouteConfigError("unsupported_tool_history")
        elif (kind != "bps" or bps_tools) and item_type == "reasoning":
            _unknown(item, {"type", "id", "summary", "encrypted_content"}, "unknown_reasoning_history_field")
            if not isinstance(item.get("encrypted_content"), str) or not item["encrypted_content"]:
                raise RouteConfigError("reasoning_history_requires_encrypted_content")
            if "summary" in item and not isinstance(item["summary"], list):
                raise RouteConfigError("invalid_reasoning_summary")
        elif kind == "siwc" and item_type == "additional_tools":
            _unknown(item, {"type", "tools"}, "unknown_input_field")
            _validate_tools(item.get("tools"), kind, nested=True)
        else:
            raise RouteConfigError("unsupported_input_item")


def _tool_json(raw: object) -> dict:
    """Strict data decoding, with no JavaScript evaluation or JSON repair."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    if not isinstance(raw, str) or len(raw) > 1048576:
        raise RouteConfigError("bps_invalid_tool_json")
    try:
        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError("object")
        _encoded(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise RouteConfigError("bps_invalid_tool_json") from None


def _bps_tools(tools: list[dict]) -> dict[str, dict]:
    _validate_tools(tools, "codex")
    result = {}
    for group in tools:
        namespace = group["name"] if group["type"] == "namespace" else None
        for tool in group["tools"] if namespace is not None else [group]:
            if tool.get("type") not in ("function", "custom"):
                raise RouteConfigError("bps_unsupported_tool_type")
            name = tool["name"]
            qualified = namespace + "." + name if namespace else name
            if qualified in result or qualified in ("run_officejs", "functions.run_officejs"):
                raise RouteConfigError("bps_ambiguous_tool_name")
            result[qualified] = {"name": name, "namespace": namespace, "type": tool["type"], "definition": tool}
    return result


def _bps_native_envelope(native: object) -> dict:
    if (not isinstance(native, dict) or native.get("type") != "function_call" or
            native.get("name") not in ("run_officejs", "functions.run_officejs") or
            any(not isinstance(native.get(key), str) or not native[key] for key in ("id", "call_id"))):
        raise RouteConfigError("bps_unexpected_native_tool")
    outer = _tool_json(native.get("arguments"))
    _unknown(outer, {"summary", "extended_summary", "destructive", "references", "code"}, "bps_unknown_native_argument")
    if (outer.get("destructive") is not False or outer.get("references") != [] or
            not isinstance(outer.get("summary"), str) or not isinstance(outer.get("extended_summary"), str)):
        raise RouteConfigError("bps_unsupported_native_tool_arguments")
    envelope = _tool_json(outer.get("code"))
    if not isinstance(envelope.get("name"), str):
        raise RouteConfigError("bps_invalid_client_tool_name")
    return envelope


def _bps_client_call(native: dict, specs: dict[str, dict]) -> dict:
    envelope = _bps_native_envelope(native)
    spec = specs.get(envelope["name"])
    if spec is None:
        raise RouteConfigError("bps_undeclared_client_tool")
    key = "arguments" if spec["type"] == "function" else "input"
    if set(envelope) != {"name", key}:
        raise RouteConfigError("bps_invalid_client_tool_envelope")
    value = envelope[key]
    if key == "arguments" and not isinstance(value, dict):
        raise RouteConfigError("bps_invalid_function_arguments")
    if key == "input" and not isinstance(value, str):
        raise RouteConfigError("bps_invalid_custom_input")
    result = {"type": "function_call" if key == "arguments" else "custom_tool_call",
              "id": native["id"], "call_id": native["call_id"], "name": spec["name"],
              key: _encoded(value).decode("utf-8") if key == "arguments" else value,
              "_bps_native": copy.deepcopy(native)}
    if spec["namespace"] is not None:
        result["namespace"] = spec["namespace"]
    return result


def replay_output(route: dict, item: dict) -> dict:
    """Build continuation history, retaining the exact native BPS call privately."""
    if not isinstance(item, dict):
        raise RouteConfigError("invalid_replay_item")
    if route.get("kind") == "bps" and item.get("type") in ("function_call", "custom_tool_call"):
        native = item.get("_bps_native")
        envelope = _bps_native_envelope(native)
        name = item.get("name")
        if isinstance(item.get("namespace"), str):
            name = item["namespace"] + "." + str(name)
        key = "arguments" if item["type"] == "function_call" else "input"
        value = _tool_json(item.get(key)) if key == "arguments" else item.get(key)
        if (item.get("call_id") != native["call_id"] or item.get("id") != native["id"] or
                envelope != {"name": name, key: value}):
            raise RouteConfigError("bps_replay_association_mismatch")
        return copy.deepcopy(native)
    result = copy.deepcopy(item)
    result.pop("status", None)
    if result.get("type") in ("message", "reasoning"):
        result.pop("id", None)
    if result.get("type") == "reasoning" and not result.get("encrypted_content"):
        raise RouteConfigError("reasoning_history_requires_encrypted_content")
    return result


def _bps_tool_contract(tools: list[dict], parallel: bool) -> str:
    batching = "Independent requests may be separate run_officejs calls in the same response." if parallel else "Request at most one client tool in a response."
    return (
        "Client tool transport for this research request: the following JSON catalog defines the available "
        "client tools and their complete schemas. Invoke a catalog tool by calling the native run_officejs "
        "function. Its arguments must have string summary and extended_summary, destructive=false, "
        "references=[], and a code STRING containing one JSON object. For a function tool that object is "
        '{"name":"namespace.tool_name","arguments":{...}}; for a custom tool it is '
        '{"name":"namespace.tool_name","input":"exact input string"}. Use the unqualified name only '
        "for a tool outside a namespace. The client intercepts the call and executes the declared tool; "
        "code is JSON data, never JavaScript or OfficeJS. Other native tools are outside this request. "
        "A result associated with that native call_id is the named client tool's result. "
        + batching + "\nComplete tool catalog:\n" + _encoded(tools).decode("utf-8")
    )


def _bps_input(items: list[dict], specs: dict[str, dict]) -> list[dict]:
    """Preserve native calls and associate each unchanged result exactly once."""
    result, calls, returned = [], {}, set()
    for item in items:
        kind = item.get("type", "message")
        if kind == "function_call":
            _bps_client_call(item, specs)
            if item["call_id"] in calls:
                raise RouteConfigError("bps_duplicate_tool_call_id")
            calls[item["call_id"]] = item
            result.append(copy.deepcopy(item))
        elif kind in ("function_call_output", "custom_tool_call_output"):
            call_id = item["call_id"]
            if call_id not in calls or call_id in returned:
                raise RouteConfigError("bps_tool_result_association_mismatch")
            returned.add(call_id)
            identifier = "fc_" + call_id
            if len(identifier) > 64:
                identifier = "fc_" + hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:61]
            result.append({"type": "function_call_output", "id": identifier, "call_id": call_id, "output": item["output"]})
        elif kind == "custom_tool_call":
            raise RouteConfigError("bps_native_replay_required")
        else:
            result.append(copy.deepcopy(item))
    if calls.keys() != returned:
        raise RouteConfigError("bps_missing_tool_result")
    return result


def _bps_metadata(session: dict, payload: dict) -> dict:
    """Private task/turn IDs live only in this route's credential snapshot."""
    items = payload["input"]
    user_positions = [index for index, item in enumerate(items) if item.get("role") == "user"]
    if not user_positions:
        raise RouteConfigError("bps_user_turn_required")
    boundary = user_positions[-1] + 1
    key = _fingerprint({"root": items[:boundary], "model": payload["model"],
                        "instructions": payload.get("instructions", ""), "tools": payload.get("tools", [])})
    tasks = session.setdefault("bps_tasks", {})
    if key not in tasks and boundary != len(items):
        raise RouteConfigError("bps_unknown_task_continuation")
    if boundary == len(items):
        # A fresh initial request starts a task; continuations reuse its IDs.
        # They never come from another route, another account, or a prior run.
        tasks[key] = {"task_id": str(uuid4()), "turn_id": str(uuid4())}
    rounds, in_results = 0, False
    for item in items[boundary:]:
        is_result = item.get("type") in ("function_call_output", "custom_tool_call_output")
        if is_result and not in_results:
            rounds += 1
        in_results = is_result
    return {**tasks[key], "agent_iteration": str(rounds + 1)}


def _bps_output(value: dict, payload: dict) -> dict:
    specs = _bps_tools(payload.get("tools", []))
    result = copy.deepcopy(value)
    output, call_ids = [], set()
    for item in result.get("output", []):
        if item.get("type") in ("function_call", "custom_tool_call"):
            item = _bps_client_call(item, specs)
            if item["call_id"] in call_ids:
                raise RouteConfigError("bps_duplicate_tool_call_id")
            call_ids.add(item["call_id"])
        output.append(item)
    if payload.get("parallel_tool_calls") is False and len(call_ids) > 1:
        raise RouteConfigError("bps_parallel_tool_contract_violation")
    result["output"] = output
    return result


def adapt_payload(route: dict, payload: dict) -> dict:
    """Validate and translate a complete request without credentials or I/O.

    Unknown capabilities fail before inference. Effort declarations are exact;
    ``max``/``ultra`` are never silently converted to another effort.
    """
    if not isinstance(route, dict) or not isinstance(route.get("kind"), str) or route["kind"] not in ENDPOINTS:
        raise RouteConfigError("invalid_route_kind")
    if not isinstance(payload, dict):
        raise RouteConfigError("payload_must_be_object")
    _encoded(payload)
    _unknown(payload, _FIELDS, "unsupported_request_field")
    kind = route["kind"]
    if not isinstance(payload.get("model"), str) or payload["model"] not in route.get("models", []):
        raise RouteConfigError("model_not_declared")
    if "store" in payload and payload["store"] is not False:
        raise RouteConfigError("store_must_be_false")
    if "stream" in payload and payload["stream"] is not True:
        raise RouteConfigError("stream_must_be_true")
    if "instructions" in payload and not isinstance(payload["instructions"], str):
        raise RouteConfigError("instructions_must_be_string")
    bps_tools = kind == "bps" and route.get("bps_tool_transport") == "run_officejs"
    _validate_input(payload.get("input"), kind, bps_tools=bps_tools)
    reasoning = payload.get("reasoning", {})
    if not isinstance(reasoning, dict):
        raise RouteConfigError("invalid_reasoning")
    _unknown(reasoning, {"effort"}, "unsupported_reasoning_field")
    if "effort" in reasoning:
        effort = reasoning["effort"]
        if not isinstance(effort, str) or effort not in _EFFORTS:
            raise RouteConfigError("invalid_reasoning_effort")
        if effort not in route.get("supported_efforts", []):
            raise RouteConfigError("reasoning_effort_not_confirmed")
    tools = payload.get("tools", [])
    if kind == "bps":
        if tools != [] and not bps_tools:
            raise RouteConfigError("bps_tool_transport_not_enabled")
        if "text" in payload:
            raise RouteConfigError("bps_capability_not_confirmed")
        if "tool_choice" in payload and payload["tool_choice"] != "auto":
            raise RouteConfigError("bps_unsupported_tool_choice")
        if "parallel_tool_calls" in payload and type(payload["parallel_tool_calls"]) is not bool:
            raise RouteConfigError("invalid_parallel_tool_calls")
        if "include" in payload and payload["include"] != ["reasoning.encrypted_content"]:
            raise RouteConfigError("bps_unsupported_include")
        specs = _bps_tools(tools)
        items = _bps_input(payload["input"], specs) if bps_tools else copy.deepcopy(payload["input"])
        prologue = []
        if payload.get("instructions"):
            prologue.append({"role": "developer", "content": payload["instructions"]})
        if specs:
            prologue.append({"role": "developer", "content": _bps_tool_contract(tools, payload.get("parallel_tool_calls", True))})
        # BPS injects native tools and sealed reasoning itself. Client tools are
        # represented by the explicit developer contract, not a top-level field.
        outgoing = {"model": payload["model"], "model_selection": "explicit", "input": prologue + items,
                    "store": False, "stream": True}
        if "effort" in reasoning:
            outgoing["reasoning_effort"] = reasoning["effort"]
        return outgoing
    _validate_tools(tools, kind)
    if "parallel_tool_calls" in payload and type(payload["parallel_tool_calls"]) is not bool:
        raise RouteConfigError("invalid_parallel_tool_calls")
    if "tool_choice" in payload and payload["tool_choice"] not in ("none", "auto", "required"):
        raise RouteConfigError("unsupported_tool_choice")
    if "include" in payload and payload["include"] != ["reasoning.encrypted_content"]:
        raise RouteConfigError("unsupported_include")
    if "text" in payload:
        value = payload["text"]
        if not isinstance(value, dict) or set(value) - {"format"} or value.get("format") != {"type": "text"}:
            raise RouteConfigError("unsupported_text_format")
    outgoing = copy.deepcopy(payload)
    outgoing.update(store=False, stream=True)
    if kind == "codex":
        outgoing.setdefault("instructions", "")
    return outgoing


def _configured_route(value: object) -> dict:
    if not isinstance(value, dict):
        raise RouteConfigError("route_must_be_object")
    required = {"id", "kind", "account_ref", "workspace_ref", "access_token_env", "expected_subject_env",
                "expected_workspace_env", "models"}
    allowed = required | {"client_id_env", "id_token_env", "account_user_id_env", "eligibility_evidence",
                          "commercial_status", "supported_efforts", "bps_tool_transport", "catalog_client_version"}
    if not required <= set(value):
        raise RouteConfigError("missing_route_field")
    _unknown(value, allowed, "unknown_route_field")
    if not isinstance(value["kind"], str) or value["kind"] not in ENDPOINTS:
        raise RouteConfigError("invalid_route_kind")
    for field in ("id", "account_ref", "workspace_ref"):
        if not isinstance(value[field], str) or not _LABEL.fullmatch(value[field]):
            raise RouteConfigError("invalid_anonymous_route_label")
    for field in required | {"client_id_env", "id_token_env", "account_user_id_env"}:
        if field.endswith("_env") and field in value:
            if not isinstance(value[field], str) or not _ENV_NAME.fullmatch(value[field]):
                raise RouteConfigError("invalid_environment_reference")
    if "id_token_env" in value and "client_id_env" not in value:
        raise RouteConfigError("id_token_requires_client_id")
    if "account_user_id_env" in value and value["kind"] != "bps":
        raise RouteConfigError("account_user_id_only_for_bps")
    if "bps_tool_transport" in value and (value["kind"] != "bps" or value["bps_tool_transport"] != "run_officejs"):
        raise RouteConfigError("invalid_bps_tool_transport")
    if "catalog_client_version" in value:
        version = value["catalog_client_version"]
        if (value["kind"] != "codex" or not isinstance(version, str) or len(version) > 64 or
                not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version)):
            raise RouteConfigError("invalid_catalog_client_version")
    models = value["models"]
    if (not isinstance(models, list) or not 1 <= len(models) <= 128 or
            any(not isinstance(model, str) or not _MODEL.fullmatch(model) for model in models) or
            len(set(models)) != len(models)):
        raise RouteConfigError("invalid_declared_models")
    efforts = value.get("supported_efforts", [])
    if not isinstance(efforts, list) or any(not isinstance(effort, str) or effort not in _EFFORTS for effort in efforts):
        raise RouteConfigError("invalid_supported_efforts")
    if value.get("commercial_status", "unknown") not in ("unknown", "confirmed", "ineligible"):
        raise RouteConfigError("invalid_commercial_status")
    if "eligibility_evidence" in value and not isinstance(value["eligibility_evidence"], dict):
        raise RouteConfigError("invalid_eligibility_evidence")
    result = copy.deepcopy(value)
    result.setdefault("commercial_status", "unknown")
    return result


def _credential(route: dict, field: str) -> str:
    value = os.environ.get(route[field])
    if (not value or len(value) > 32768 or value != value.strip() or
            any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise RouteConfigError("missing_or_malformed_" + field)
    return value


def _recent_eligibility(route: dict) -> bool:
    evidence = route.get("eligibility_evidence", {})
    available = evidence.get("official_excel_available") is True or evidence.get("bps_route_available") is True
    if not available or evidence.get("same_account_confirmed") is not True:
        return False
    try:
        observed = datetime.fromisoformat(evidence["observed_at"].replace("Z", "+00:00"))
        if observed.tzinfo is None:
            return False
        age = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
        return 0 <= age <= 86400
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return False


def _usage(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    result = {key: value[key] for key in ("input_tokens", "output_tokens", "total_tokens")
              if type(value.get(key)) is int and value[key] >= 0}
    for key, detail in (("input_tokens_details", "cached_tokens"), ("output_tokens_details", "reasoning_tokens")):
        if isinstance(value.get(key), dict) and type(value[key].get(detail)) is int and value[key][detail] >= 0:
            result[key] = {detail: value[key][detail]}
    return result or None


def _code(value: object) -> str | None:
    return value if isinstance(value, str) and _CODE.fullmatch(value) else None


def _error(value: dict, fallback: str) -> tuple[str, str | None]:
    error = value.get("error")
    if not isinstance(error, dict):
        error = value
    return _code(error.get("code")) or fallback, _code(error.get("type"))


def _redact(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "error" and isinstance(item, dict):
                code, error_type = _error({"error": item}, "upstream_error")
                item = {"code": code, "type": error_type}
            result[key] = _redact(item, secrets)
        return result
    return value


def _public_response(value: dict, secrets: tuple[str, ...]) -> dict:
    # Whitelist top-level response fields. In particular, discard request echoes,
    # metadata and raw error messages. Keep sealed reasoning in memory for exact
    # tool-history replay; callers persist only the parsed public answer.
    result = {key: value[key] for key in ("id", "object", "status", "model") if isinstance(value.get(key), str)}
    output = value.get("output", [])
    result["output"] = []
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "reasoning":
                item = {key: item[key] for key in ("type", "id", "summary", "encrypted_content") if key in item}
            result["output"].append(item)
    result["usage"] = _usage(value.get("usage"))
    if isinstance(value.get("error"), dict):
        code, error_type = _error(value, "upstream_failed")
        result["error"] = {"code": code, "type": error_type}
    incomplete = value.get("incomplete_details")
    if isinstance(incomplete, dict):
        result["incomplete_details"] = {"reason": _code(incomplete.get("reason"))}
    return _redact(result, secrets)


class _NoCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request) -> bool:
        return False

    def return_ok(self, cookie, request) -> bool:
        return False


class ResearchTransport:
    """Explicit route lifecycle; one immutable credential snapshot per preflight."""

    def __init__(self, routes: list[dict], *, timeout_seconds: float = 180, max_bytes: int = 8388608):
        if not isinstance(routes, list) or not routes:
            raise RouteConfigError("routes_must_be_nonempty_array")
        parsed = [_configured_route(route) for route in routes]
        if len({route["id"] for route in parsed}) != len(parsed):
            raise RouteConfigError("duplicate_route_id")
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 3600:
            raise RouteConfigError("invalid_timeout")
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 67108864:
            raise RouteConfigError("invalid_max_bytes")
        self._routes = {route["id"]: route for route in parsed}
        self._timeout = float(timeout_seconds)
        self._max_bytes = max_bytes
        self._client = None
        self._httpx = None
        self._jwt = None
        self._jwks = None
        self._preflights: dict[str, dict] = {}
        self._preflight_locks: dict[str, asyncio.Lock] = {}
        self._sessions: dict[str, dict] = {}
        self._busy: set[str] = set()

    async def __aenter__(self) -> ResearchTransport:
        if self._client is not None:
            raise RouteConfigError("transport_already_open")
        try:
            import httpx
            import jwt
        except ImportError:
            raise RouteConfigError("research_dependency_missing") from None
        self._httpx, self._jwt = httpx, jwt
        self._preflight_locks = {route_id: asyncio.Lock() for route_id in self._routes}
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout), follow_redirects=False, trust_env=False,
            cookies=CookieJar(policy=_NoCookies()), transport=httpx.AsyncHTTPTransport(retries=0),
            headers={"Accept-Encoding": "identity", "User-Agent": "fenjue-route-research/0.1"},
        )
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            if self._client is not None:
                await self._client.aclose()
        finally:
            self._client = None
            self._sessions.clear()
            self._preflights.clear()
            self._preflight_locks.clear()
            self._jwks = None
            self._busy.clear()

    def _route(self, route_id: str) -> dict:
        if not isinstance(route_id, str) or route_id not in self._routes:
            raise RouteConfigError("unknown_route_id")
        if self._client is None:
            raise RouteConfigError("transport_not_open")
        return self._routes[route_id]

    async def _body(self, response) -> bytes:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > self._max_bytes:
                raise _RouteFailure("upstream_response_limit")
            body.extend(chunk)
        return bytes(body)

    async def _get_json(self, url: str, headers: dict | None = None) -> dict:
        from ..upstream import _json_object, UpstreamError

        async with self._client.stream("GET", url, headers=headers or {}) as response:
            if response.status_code != 200:
                raise _RouteFailure("preflight_http_error", http_status=response.status_code)
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise _RouteFailure("unsupported_upstream_encoding")
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json" and not content_type.endswith("+json"):
                raise _RouteFailure("unexpected_upstream_content")
            try:
                return _json_object(await self._body(response))
            except UpstreamError as exc:
                raise _RouteFailure(exc.code) from None

    async def _keys(self) -> list[dict]:
        if self._jwks is None:
            discovery = await self._get_json(_ISSUER + "/.well-known/openid-configuration")
            if discovery.get("issuer") != _ISSUER:
                raise _RouteFailure("unexpected_identity_issuer")
            uri = discovery.get("jwks_uri")
            if not isinstance(uri, str):
                raise _RouteFailure("missing_jwks_uri")
            try:
                parsed = urlsplit(uri)
                valid = (parsed.scheme == "https" and parsed.hostname == "auth.openai.com" and
                         parsed.port in (None, 443) and not parsed.username and not parsed.password and
                         not parsed.query and not parsed.fragment and not "\\" in uri)
            except ValueError:
                valid = False
            if not valid:
                raise _RouteFailure("unexpected_jwks_origin")
            keys = (await self._get_json(uri)).get("keys")
            if not isinstance(keys, list) or not 1 <= len(keys) <= 64 or any(not isinstance(key, dict) for key in keys):
                raise _RouteFailure("invalid_identity_keys")
            self._jwks = keys
        return self._jwks

    async def _claims(self, token: str, audience: str) -> dict:
        try:
            header = self._jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            if algorithm not in {"RS256", "ES256"} or not isinstance(header.get("kid"), str):
                raise _RouteFailure("unsupported_token_signature")
            keys = [key for key in await self._keys() if key.get("kid") == header["kid"]]
            if len(keys) != 1:
                raise _RouteFailure("token_signing_key_unavailable")
            key = keys[0]
            if key.get("use", "sig") != "sig" or key.get("alg", algorithm) != algorithm:
                raise _RouteFailure("invalid_token_signing_key")
            signing_key = self._jwt.PyJWK.from_dict(key, algorithm=algorithm).key
            claims = self._jwt.decode(token, signing_key, algorithms=[algorithm], audience=audience,
                                      issuer=_ISSUER, leeway=0,
                                      options={"require": ["exp", "iss", "aud", "sub"], "verify_signature": True})
            if (not isinstance(claims.get("sub"), str) or not claims["sub"] or
                    type(claims.get("exp")) is not int or
                    any(field in claims and type(claims[field]) is not int for field in ("iat", "nbf"))):
                raise _RouteFailure("invalid_identity_claims")
            return claims
        except self._jwt.ExpiredSignatureError:
            raise _RouteFailure("token_expired") from None
        except self._jwt.PyJWTError:
            raise _RouteFailure("token_validation_failed") from None
        except (ValueError, TypeError, KeyError):
            raise _RouteFailure("invalid_identity_key_or_token") from None

    async def _identity(self, route: dict) -> tuple[dict, dict]:
        token = _credential(route, "access_token_env")
        subject = _credential(route, "expected_subject_env")
        workspace = _credential(route, "expected_workspace_env")
        client_id = _credential(route, "client_id_env") if "client_id_env" in route else None
        id_token = _credential(route, "id_token_env") if "id_token_env" in route else None
        account_user = _credential(route, "account_user_id_env") if "account_user_id_env" in route else None
        claims = await self._claims(token, _AUDIENCE)
        if not _same_identity(claims["sub"], subject):
            raise _RouteFailure("subject_mismatch")
        if client_id is not None and claims.get("client_id") != client_id:
            raise _RouteFailure("client_id_mismatch")
        if route["kind"] == "siwc":
            scope = claims.get("scope")
            if not isinstance(scope, str) or "chatgpt.tokens.use.direct" not in scope.split():
                raise _RouteFailure("siwc_scope_missing")
        id_claims = await self._claims(id_token, client_id) if id_token is not None else None
        if id_claims is not None and id_claims["sub"] != claims["sub"]:
            raise _RouteFailure("id_token_subject_mismatch")
        # SIWC documents encrypted metadata as opaque; it is never decoded.
        workspaces = []
        for value in (claims, id_claims):
            if value is None:
                continue
            auth = value.get(_AUTH_CLAIM)
            if isinstance(auth, dict) and isinstance(auth.get("chatgpt_account_id"), str):
                workspaces.append(auth["chatgpt_account_id"])
        if any(not _same_identity(item, workspace) for item in workspaces):
            raise _RouteFailure("workspace_mismatch")
        verified = bool(workspaces)
        status = "verified" if verified else "operator_attested" if _recent_eligibility(route) else "workspace_unverified"
        identity = {"status": status, "signature_verified": True, "subject_verified": True,
                    "workspace_verified": verified, "subject_hash": _hash_identity("subject", claims["sub"]),
                    "workspace_hash": _hash_identity("workspace", workspace) if verified else None,
                    "expected_workspace_hash": _hash_identity("workspace", workspace)}
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        if route["kind"] in {"codex", "bps"}:
            headers["ChatGPT-Account-Id"] = workspace
        if route["kind"] == "bps":
            headers["X-Basispoints-Auth-Mode"] = "chatgpt"
            headers["X-OpenAI-Account-Id"] = workspace
            if account_user is not None:
                auth = claims.get(_AUTH_CLAIM, {})
                if not isinstance(auth, dict) or auth.get("chatgpt_account_user_id") != account_user:
                    raise _RouteFailure("account_user_id_unverified")
                headers["X-OpenAI-Account-User-Id"] = account_user
        secrets = tuple(value for value in (token, id_token) if value is not None)
        return identity, {"headers": headers, "secrets": secrets, "expires_at": claims["exp"]}

    @staticmethod
    def _catalog(value: dict) -> list[dict]:
        raw = value.get("models")
        if not isinstance(raw, list):
            raise _RouteFailure("unsupported_catalog_schema")
        result = []
        for model in raw:
            if not isinstance(model, dict):
                raise _RouteFailure("unsupported_catalog_schema")
            identifier = model.get("slug")
            if not isinstance(identifier, str) or not _MODEL.fullmatch(identifier):
                raise _RouteFailure("unsupported_catalog_model")
            efforts = model.get("supported_reasoning_levels", model.get("supported_reasoning_efforts", []))
            if not isinstance(efforts, list):
                raise _RouteFailure("unsupported_catalog_efforts")
            normalized = []
            for effort in efforts:
                level = effort.get("effort") if isinstance(effort, dict) else effort
                if isinstance(level, str) and level in _EFFORTS:
                    normalized.append(level)
            result.append({"id": identifier, "supported_efforts": list(dict.fromkeys(normalized)),
                           "visibility": model.get("visibility") if model.get("visibility") in ("list", "hide") else None})
        return result

    async def preflight(self, route_id: str) -> dict:
        """Validate one explicitly configured credential and read its catalog.

        JWKS discovery uses only auth.openai.com; failure has no unverified-token
        fallback. Results and credentials are cached for this context manager.
        """
        self._route(route_id)
        async with self._preflight_locks[route_id]:
            return await self._preflight(route_id)

    async def _preflight(self, route_id: str) -> dict:
        route = self._route(route_id)
        if route_id in self._preflights:
            return copy.deepcopy(self._preflights[route_id])
        result = {"route_id": route_id, "kind": route["kind"], "account_ref": route["account_ref"],
                  "workspace_ref": route["workspace_ref"], "endpoint": ENDPOINTS[route["kind"]],
                  "commercial_status": route["commercial_status"], "status": "unavailable", "available": False,
                  "error_code": None, "http_status": None,
                  "identity": {"status": "unverified", "signature_verified": False, "subject_verified": False,
                               "workspace_verified": False, "subject_hash": None, "workspace_hash": None},
                  "catalog": {"status": "unavailable", "models": []}}
        if route["kind"] == "bps" and not _recent_eligibility(route):
            result["error_code"] = "bps_current_availability_evidence_required"
            self._preflights[route_id] = result
            return copy.deepcopy(result)
        try:
            async with asyncio.timeout(self._timeout):
                identity, session = await self._identity(route)
                result["identity"] = identity
                if route["kind"] == "bps":
                    catalog = [{"id": model, "supported_efforts": route.get("supported_efforts", [])}
                               for model in route["models"]]
                    catalog_status = "operator_declared"
                else:
                    catalog_url = CATALOGS[route["kind"]]
                    if route.get("catalog_client_version"):
                        # Official ModelsClient supplies this compatibility query.
                        # The operator supplies its value; no client is imitated.
                        catalog_url += "?" + urlencode({"client_version": route["catalog_client_version"]})
                    catalog = self._catalog(await self._get_json(catalog_url, session["headers"]))
                    catalog_status = "verified"
                result["catalog"] = {"status": catalog_status, "models": catalog}
                if route.get("catalog_client_version"):
                    result["catalog"]["client_version"] = route["catalog_client_version"]
                session["catalog"] = {model["id"]: model for model in catalog}
                self._sessions[route_id] = session
                result.update(status="ready", available=True)
        except _RouteFailure as exc:
            result.update(error_code=exc.code, http_status=exc.http_status)
        except RouteConfigError as exc:
            result["error_code"] = exc.code
        except (TimeoutError, self._httpx.TimeoutException):
            result["error_code"] = "preflight_timeout"
        except self._httpx.HTTPError:
            result["error_code"] = "preflight_network_error"
        self._preflights[route_id] = result
        return copy.deepcopy(result)

    async def call(self, route_id: str, payload: dict) -> dict:
        """Make at most one inference POST, returning sanitized terminal evidence."""
        route = self._route(route_id)
        if route_id in self._busy:
            raise RouteConfigError("concurrent_route_call")
        preflight = await self.preflight(route_id)
        if route_id in self._busy:
            raise RouteConfigError("concurrent_route_call")
        started = time.monotonic()
        result = {"response": None, "status": "unavailable", "error_code": preflight["error_code"],
                  "error_type": None, "http_status": None, "content_type": None, "request_id": None,
                  "usage": None, "latency_seconds": 0.0,
                  "requested_model": payload.get("model") if isinstance(payload, dict) else None,
                  "reported_model": None, "model_mismatch": None, "identity": preflight["identity"],
                  "request_fingerprint": _fingerprint(payload)}
        if not preflight["available"]:
            return result
        session = self._sessions[route_id]
        if session["expires_at"] <= time.time():
            result.update(status="authentication_error", error_code="token_expired")
            return result
        if route["kind"] == "bps" and not _recent_eligibility(route):
            result["error_code"] = "bps_current_availability_evidence_required"
            return result
        model = payload.get("model") if isinstance(payload, dict) else None
        if not isinstance(model, str) or model not in session["catalog"]:
            raise RouteConfigError("model_absent_from_current_catalog")
        effective_route = copy.deepcopy(route)
        catalog_efforts = session["catalog"][model]["supported_efforts"]
        if not effective_route.get("supported_efforts"):
            effective_route["supported_efforts"] = catalog_efforts
        elif catalog_efforts:
            effective_route["supported_efforts"] = [effort for effort in effective_route["supported_efforts"] if effort in catalog_efforts]
        outgoing = adapt_payload(effective_route, payload)
        if route["kind"] == "bps" and route.get("bps_tool_transport") == "run_officejs" and payload.get("tools"):
            outgoing["metadata"] = _bps_metadata(session, payload)
        encoded = _encoded(outgoing)
        if len(encoded) > self._max_bytes:
            raise RouteConfigError("request_byte_limit")
        result["request_fingerprint"] = hashlib.sha256(encoded).hexdigest()
        headers = {**session["headers"], "Accept": "text/event-stream", "Content-Type": "application/json"}
        from ..upstream import _json_object, _sse_objects, UpstreamError

        self._busy.add(route_id)
        try:
            async with asyncio.timeout(self._timeout):
                async with self._client.stream("POST", ENDPOINTS[route["kind"]], content=encoded, headers=headers) as response:
                    result["http_status"] = response.status_code
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    result["content_type"] = content_type if re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", content_type) else None
                    request_id = response.headers.get("x-request-id", response.headers.get("request-id"))
                    result["request_id"] = request_id if isinstance(request_id, str) and _MODEL.fullmatch(request_id) else None
                    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                        raise _RouteFailure("unsupported_upstream_encoding")
                    if not 200 <= response.status_code < 300:
                        result["status"] = "http_error"
                        body = await self._body(response)
                        fallback = "upstream_authentication" if response.status_code in {401, 403} else "upstream_rate_limited" if response.status_code == 429 else "upstream_http_error"
                        result["error_code"] = fallback
                        if content_type == "application/json" or content_type.endswith("+json"):
                            try:
                                value = _json_object(body)
                                result["error_code"], result["error_type"] = _error(value, fallback)
                                result["usage"] = _usage(value.get("usage"))
                            except UpstreamError:
                                pass
                        result["latency_seconds"] = round(time.monotonic() - started, 6)
                        return _redact(result, session["secrets"])
                    terminal = None
                    terminal_event = None
                    if content_type == "text/event-stream":
                        async for event in _sse_objects(response, self._max_bytes):
                            event_type = event.get("type")
                            if not isinstance(event_type, str):
                                raise _RouteFailure("invalid_upstream_event_type")
                            if event_type == "error":
                                result["usage"] = _usage(event.get("usage"))
                                code, error_type = _error(event, "upstream_stream_error")
                                raise _RouteFailure(code, error_type=error_type)
                            if route["kind"] == "siwc" and event_type in {"response.done", "response.finished"}:
                                alternate = event.get("response")
                                if isinstance(alternate, dict):
                                    result["usage"] = _usage(alternate.get("usage"))
                                raise _RouteFailure("siwc_unsupported_terminal_event")
                            if event_type in {"response.completed", "response.failed", "response.incomplete", "response.done", "response.finished"}:
                                terminal = event.get("response")
                                terminal_event = event_type
                                if not isinstance(terminal, dict):
                                    raise _RouteFailure("invalid_upstream_terminal")
                                if event_type in {"response.completed", "response.failed", "response.incomplete"} and terminal.get("status") != event_type.removeprefix("response."):
                                    raise _RouteFailure("conflicting_terminal_status")
                                break
                    elif content_type == "application/json" or content_type.endswith("+json"):
                        terminal = _json_object(await self._body(response))
                    else:
                        raise _RouteFailure("unexpected_upstream_content")
                    if not isinstance(terminal, dict) or terminal.get("status") not in ("completed", "failed", "incomplete"):
                        raise _RouteFailure("missing_terminal_status")
                    status = terminal["status"]
                    result["usage"] = _usage(terminal.get("usage"))
                    result["reported_model"] = terminal.get("model") if isinstance(terminal.get("model"), str) else None
                    result["model_mismatch"] = (result["reported_model"] != model) if result["reported_model"] is not None else None
                    if route["kind"] == "siwc" and status == "completed" and terminal_event != "response.completed":
                        raise _RouteFailure("siwc_success_requires_completed_event")
                    if status != "failed" and (not isinstance(terminal.get("output"), list) or any(not isinstance(item, dict) for item in terminal["output"])):
                        raise _RouteFailure("invalid_upstream_output")
                    if route["kind"] == "bps" and status != "failed":
                        try:
                            terminal = _bps_output(terminal, payload)
                        except RouteConfigError as exc:
                            raise _RouteFailure(exc.code) from None
                    result.update(status=status, response=_public_response(terminal, session["secrets"]))
                    if status == "failed":
                        result["error_code"], result["error_type"] = _error(terminal, "upstream_failed")
                    elif status == "incomplete":
                        details = terminal.get("incomplete_details")
                        result["error_code"] = (_code(details.get("reason")) if isinstance(details, dict) else None) or "upstream_incomplete"
                    elif result["reported_model"] is None:
                        result.update(status="protocol_error", error_code="missing_upstream_model")
        except _RouteFailure as exc:
            result.update(status="protocol_error", error_code=exc.code, error_type=exc.error_type)
        except UpstreamError as exc:
            result.update(status="protocol_error", error_code=exc.code)
        except (TimeoutError, self._httpx.TimeoutException):
            result.update(status="timeout", error_code="upstream_timeout")
        except self._httpx.HTTPError:
            result.update(status="network_error", error_code="upstream_network_error")
        finally:
            self._busy.discard(route_id)
            result["latency_seconds"] = round(time.monotonic() - started, 6)
        return _redact(result, session["secrets"])
