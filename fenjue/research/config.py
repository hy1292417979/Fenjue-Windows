"""Study configuration is an immutable declaration, never a credential store."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
CAPS = {"total": 300, "eligibility": 20, "screen": 80, "confirm": 200}


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("Nonfinite JSON value")


def read_json(path: Path, max_bytes: int = 32_000_000) -> Any:
    with path.open("rb") as source:
        data = source.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("JSON file exceeds its size limit")
    result = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_constant=_constant)
    canonical(result)  # Reject overflowed floats as well as explicit NaN.
    return result


def _label(value, field):
    if not isinstance(value, str) or not LABEL.fullmatch(value):
        raise ValueError(f"{field} requires an anonymous ASCII identifier")
    return value


def load_config(path: Path) -> dict:
    raw = read_json(path, 262144)
    if not isinstance(raw, dict):
        raise ValueError("Study configuration must be an object")
    if set(raw) - {"study_id", "seed", "budget", "timeout_seconds", "window_min_seconds", "routes", "comparisons"}:
        raise ValueError("Unknown study configuration field")
    config = {"study_id": _label(raw.get("study_id"), "study_id")}
    seed = raw.get("seed", 20261007)
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed must be an unsigned 32-bit integer")
    config["seed"] = seed
    budget = raw.get("budget", CAPS)
    if not isinstance(budget, dict) or set(budget) != set(CAPS):
        raise ValueError("budget requires total, eligibility, screen, and confirm")
    if any(type(budget[key]) is not int or not 1 <= budget[key] <= cap for key, cap in CAPS.items()):
        raise ValueError("Budget exceeds the authorized 300/20/80/200 limits or is invalid")
    config["budget"] = dict(budget)
    timeout = raw.get("timeout_seconds", 180)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 600:
        raise ValueError("timeout_seconds must be from 1 through 600")
    config["timeout_seconds"] = timeout
    separation = raw.get("window_min_seconds", 21600)
    if type(separation) is not int or not 21600 <= separation <= 604800:
        raise ValueError("Confirmation windows must be at least six hours apart")
    config["window_min_seconds"] = separation
    routes = raw.get("routes")
    if not isinstance(routes, list) or not 2 <= len(routes) <= 256:
        raise ValueError("routes must contain 2-256 explicit entries")
    allowed = {"id", "kind", "account_ref", "workspace_ref", "access_token_env", "expected_subject_env",
               "expected_workspace_env", "client_id_env", "id_token_env", "account_user_id_env",
               "models", "supported_efforts", "eligibility_evidence", "commercial_status", "bps_tool_transport", "catalog_client_version"}
    route_map = {}
    for route in routes:
        if not isinstance(route, dict) or set(route) - allowed:
            raise ValueError("Unknown route field; credentials belong in referenced environment variables")
        route = dict(route)
        for key in ("id", "account_ref", "workspace_ref"):
            _label(route.get(key), f"route.{key}")
        if route["id"] in route_map:
            raise ValueError("Duplicate route id")
        if route.get("kind") not in ("codex", "siwc", "bps"):
            raise ValueError("Supported research route kinds: codex, siwc, bps")
        for key in ("access_token_env", "expected_subject_env", "expected_workspace_env"):
            if not isinstance(route.get(key), str) or not ENV.fullmatch(route[key]):
                raise ValueError(f"route.{key} must reference an environment variable")
        for key in ("client_id_env", "id_token_env", "account_user_id_env"):
            if key in route and (not isinstance(route[key], str) or not ENV.fullmatch(route[key])):
                raise ValueError(f"route.{key} must reference an environment variable")
        models = route.get("models")
        if not isinstance(models, list) or not models or len(models) > 128:
            raise ValueError("Route models must be an explicit nonempty list")
        if any(not isinstance(model, str) or not MODEL.fullmatch(model) for model in models):
            raise ValueError("Invalid model name")
        if len(set(models)) != len(models) or "*" in models:
            raise ValueError("Model lists must be unique and explicit")
        efforts = route.get("supported_efforts", [])
        if not isinstance(efforts, list) or any(not isinstance(effort, str) or effort not in EFFORTS for effort in efforts):
            raise ValueError("supported_efforts must contain explicit strings")
        route["supported_efforts"] = efforts
        if route.get("commercial_status", "unknown") not in ("unknown", "confirmed", "ineligible"):
            raise ValueError("Invalid commercial_status")
        route.setdefault("commercial_status", "unknown")
        evidence = route.get("eligibility_evidence", {})
        if not isinstance(evidence, dict) or set(evidence) - {"official_excel_available", "bps_route_available", "same_account_confirmed", "observed_at"}:
            raise ValueError("Invalid eligibility_evidence; supply only availability, identity and observation time")
        for key in ("official_excel_available", "bps_route_available", "same_account_confirmed"):
            if key in evidence and type(evidence[key]) is not bool:
                raise ValueError("Evidence flags must be boolean")
        if "observed_at" in evidence and (not isinstance(evidence["observed_at"], str) or len(evidence["observed_at"]) > 40):
            raise ValueError("Evidence observed_at must be an ISO timestamp")
        if "id_token_env" in route and "client_id_env" not in route:
            raise ValueError("id_token_env requires client_id_env")
        if "account_user_id_env" in route and route["kind"] != "bps":
            raise ValueError("account_user_id_env is specific to BPS")
        if "bps_tool_transport" in route and (route["kind"] != "bps" or route["bps_tool_transport"] != "run_officejs"):
            raise ValueError("bps_tool_transport must explicitly select run_officejs on a BPS route")
        if "catalog_client_version" in route and (route["kind"] != "codex" or not isinstance(route["catalog_client_version"], str)
                                                   or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", route["catalog_client_version"])):
            raise ValueError("catalog_client_version requires a known Codex semantic version")
        route_map[route["id"]] = route
    comparisons = raw.get("comparisons")
    if not isinstance(comparisons, list) or not 1 <= len(comparisons) <= 2:
        raise ValueError("This 300-call study supports one or two declared comparisons")
    seen = set()
    resolved = []
    for comparison in comparisons:
        if not isinstance(comparison, dict) or set(comparison) - {"id", "baseline", "candidate", "model", "reasoning"}:
            raise ValueError("Invalid comparison fields")
        _label(comparison.get("id"), "comparison.id")
        if comparison["id"] in seen:
            raise ValueError("Duplicate comparison id")
        seen.add(comparison["id"])
        baseline = route_map.get(comparison.get("baseline")) if isinstance(comparison.get("baseline"), str) else None
        candidate = route_map.get(comparison.get("candidate")) if isinstance(comparison.get("candidate"), str) else None
        if not baseline or not candidate or baseline["id"] == candidate["id"]:
            raise ValueError("Comparison requires two distinct configured routes")
        if (baseline["account_ref"], baseline["workspace_ref"]) != (candidate["account_ref"], candidate["workspace_ref"]):
            raise ValueError("Both arms must declare the same account and workspace")
        model = comparison.get("model")
        if model not in baseline["models"] or model not in candidate["models"]:
            raise ValueError("Comparison model must be listed on both routes")
        reasoning = comparison.get("reasoning", {})
        if not isinstance(reasoning, dict) or set(reasoning) - {"effort"}:
            raise ValueError("Unsupported comparison reasoning fields")
        if any(not isinstance(value, str) or not value or len(value) > 64 for value in reasoning.values()):
            raise ValueError("Reasoning values must be explicit strings")
        if "effort" in reasoning and any(reasoning["effort"] not in route["supported_efforts"] for route in (baseline, candidate)):
            raise ValueError("Declare the selected effort in supported_efforts on both routes")
        resolved.append({**comparison, "reasoning": reasoning, "account_ref": baseline["account_ref"], "workspace_ref": baseline["workspace_ref"]})
    config["routes"] = list(route_map.values())
    config["comparisons"] = resolved
    return config
