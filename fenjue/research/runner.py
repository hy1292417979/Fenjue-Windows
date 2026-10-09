"""Single-answer paired runs with full accounting and no retry or account fallback."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from .config import canonical, digest, _pairs, _constant
from .routes import ResearchTransport, RouteConfigError, adapt_payload, replay_output
from .storage import BudgetExhausted, StudyDB, now
from .study import reporting_manifest
from .tasks import execute_tool, grade, initial_payload


def outcome_key(row: dict) -> str:
    return row["pair_id"] + ":" + row["arm"]


def identity_matches(left: dict, right: dict) -> bool:
    a, b = left.get("identity", {}), right.get("identity", {})
    return bool(
        a.get("signature_verified") and b.get("signature_verified")
        and a.get("subject_verified") and b.get("subject_verified")
        and a.get("workspace_verified") and b.get("workspace_verified")
        and a.get("subject_hash") and a.get("workspace_hash")
        and a.get("subject_hash") == b.get("subject_hash")
        and a.get("workspace_hash") == b.get("workspace_hash")
    )


def pin_identity(db: StudyDB, route: dict, info: dict) -> None:
    identity = info.get("identity", {})
    if not (identity.get("signature_verified") and identity.get("subject_verified") and identity.get("workspace_verified")):
        return
    pins = {
        "account_pin:" + route["account_ref"]: identity["subject_hash"],
        "workspace_pin:" + route["account_ref"] + ":" + route["workspace_ref"]: identity["workspace_hash"],
    }
    for key, value in pins.items():
        previous = db.metadata(key)
        if previous is not None and previous != value:
            raise ValueError("Verified identity differs from the frozen study identity; account switching stopped")
        if previous is None:
            db.set_metadata_once(key, value)


def final_text(response: dict) -> str:
    text = []
    for item in response.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message" or item.get("phase") not in (None, "final_answer"):
            continue
        for part in item.get("content", []):
            if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                text.append(part["text"])
    return "\n".join(text)


def summed_usage(envelopes: list[dict]) -> dict | None:
    if not envelopes:
        return None
    sums = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    details = {"cached_tokens": 0, "reasoning_tokens": 0}
    for envelope in envelopes:
        usage = envelope.get("usage")
        if not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0 for key in sums):
            return None
        if usage["input_tokens"] + usage["output_tokens"] != usage["total_tokens"]:
            return None
        for key in sums:
            sums[key] += usage[key]
        for container, name in (("input_tokens_details", "cached_tokens"), ("output_tokens_details", "reasoning_tokens")):
            group = usage.get(container)
            value = group.get(name) if isinstance(group, dict) else None
            if type(value) is not int or value < 0:
                details[name] = None
            elif details[name] is not None:
                details[name] += value
    for container, name in (("input_tokens_details", "cached_tokens"), ("output_tokens_details", "reasoning_tokens")):
        if details[name] is not None:
            sums[container] = {name: details[name]}
    return sums


def safe_envelope(envelope: dict) -> dict:
    keys = ("status", "error_code", "error_type", "http_status", "content_type", "request_id", "usage",
            "latency_seconds", "requested_model", "reported_model", "identity", "request_fingerprint", "model_mismatch")
    return {key: envelope.get(key) for key in keys}


async def charged_call(db: StudyDB, transport: ResearchTransport, stage: str, route_id: str, key: str, payload: dict, route: dict) -> dict:
    # Schema failures occur before budget reservation and before any network POST.
    actual = adapt_payload(route, payload)
    call_id = db.charge(stage, route_id, key)
    try:
        envelope = await transport.call(route_id, payload)
    except asyncio.CancelledError:
        db.finish_call(call_id, {"status": "interrupted_unknown", "usage": None})
        raise
    except RouteConfigError as error:
        envelope = {"status": "unsupported_request", "error_code": error.code, "usage": None}
    except Exception:
        # Never log raw upstream exception text or retry an ambiguous submission.
        envelope = {"status": "transport_exception", "error_code": "transport_exception", "usage": None}
    audit = safe_envelope(envelope)
    audit["actual_parameters"] = {key: value for key, value in actual.items() if key != "input"}
    audit["input_fingerprint"] = digest(actual.get("input"))
    db.finish_call(call_id, audit)
    return envelope


async def inspect_routes(config: dict, db: StudyDB, *, call_readiness: bool = False) -> dict:
    snapshot = {"observed_at": now(), "routes": [], "comparisons": []}
    async with ResearchTransport(config["routes"], timeout_seconds=config["timeout_seconds"]) as transport:
        preflight = {}
        for route in config["routes"]:
            info = await transport.preflight(route["id"])
            pin_identity(db, route, info)
            preflight[route["id"]] = info
            snapshot["routes"].append(info)
        for comparison in config["comparisons"]:
            pair = {"comparison_id": comparison["id"], "same_identity_verified": identity_matches(preflight[comparison["baseline"]], preflight[comparison["candidate"]]), "model": comparison["model"]}
            snapshot["comparisons"].append(pair)
        if call_readiness:
            routes = {route["id"]: route for route in config["routes"]}
            attempted = set()
            for comparison in config["comparisons"]:
                if (not all(preflight[comparison[arm]].get("available") for arm in ("baseline", "candidate"))
                        or not identity_matches(preflight[comparison["baseline"]], preflight[comparison["candidate"]])):
                    continue
                for arm in ("baseline", "candidate"):
                    route_id = comparison[arm]
                    key = "eligibility:" + route_id + ":" + comparison["model"]
                    if key in attempted or db.has_attempt(key):
                        continue
                    attempted.add(key)
                    payload = {"model": comparison["model"], "input": [{"role": "user", "content": "Return exactly FENJUE_READY."}],
                               "instructions": "Complete the requested text response.", "store": False, "stream": True}
                    if comparison["reasoning"]:
                        payload["reasoning"] = comparison["reasoning"]
                    try:
                        result = await charged_call(db, transport, "eligibility", route_id, key, payload, routes[route_id])
                        pair = {"route_id": route_id, "model": comparison["model"], **safe_envelope(result)}
                        snapshot.setdefault("readiness", []).append(pair)
                    except RouteConfigError as error:
                        snapshot.setdefault("readiness", []).append({"route_id": route_id, "status": "unsupported", "error_code": getattr(error, "code", "request_contract")})
                    except BudgetExhausted:
                        snapshot["budget_exhausted"] = True
                        return snapshot
    return snapshot


async def _one_task(task: dict, row: dict, comparison: dict, route: dict, transport: ResearchTransport,
                    db: StudyDB, config: dict, identity_verified: bool) -> dict:
    started = time.monotonic()
    record = {**row, "timestamp": now(), "status": "pending", "passed": False, "score": 0.0,
              "requested_model": comparison["model"], "reported_model": None, "model_match": False,
              "identity_verified": identity_verified, "call_count": 0, "latency_seconds": 0.0, "usage": None}
    payload = initial_payload(task, comparison["model"], comparison["reasoning"])
    if task.get("tools") and route["kind"] != "bps":
        payload["include"] = ["reasoning.encrypted_content"]
    envelopes, tool_trace = [], []
    tool_state = {"namespace": "fixture"}
    answer = ""
    try:
        for turn in range(task["max_turns"]):
            if time.monotonic() - started >= config["timeout_seconds"]:
                record["status"] = "task_deadline"
                break
            remaining = config["timeout_seconds"] - (time.monotonic() - started)
            async with asyncio.timeout(remaining):
                envelope = await charged_call(db, transport, row["split"], row["route_id"], outcome_key(row), payload, route)
            envelopes.append(envelope)
            record["status"] = envelope.get("status", "upstream_error")
            observed = [item for item in envelopes if item.get("reported_model") is not None or item.get("status") == "completed"]
            record["reported_model"] = observed[-1].get("reported_model") if observed else None
            record["model_match"] = bool(observed) and all(item.get("reported_model") == comparison["model"] for item in observed)
            response = envelope.get("response")
            if envelope.get("status") != "completed" or not isinstance(response, dict):
                record["error_code"] = envelope.get("error_code")
                break
            calls = [item for item in response.get("output", []) if isinstance(item, dict) and item.get("type") == "function_call"]
            if not calls:
                answer = final_text(response)
                verdict = grade(task, answer, tool_trace)
                record.update({"passed": bool(verdict["passed"]), "score": verdict["score"], "grade_reason": verdict["reason"]})
                if "tool_evidence" in verdict:
                    record["tool_evidence"] = verdict["tool_evidence"]
                break
            if not task.get("tools") or len(calls) > 16 or turn + 1 >= task["max_turns"]:
                record["status"] = "tool_limit"
                break
            replay = [replay_output(route, item) for item in response["output"] if isinstance(item, dict)]
            seen_call_ids = set()
            results = []
            for call in calls:
                call_id = call.get("call_id")
                if not isinstance(call_id, str) or not call_id or call_id in seen_call_ids:
                    raise ValueError("Invalid tool association")
                seen_call_ids.add(call_id)
                name = call.get("name")
                namespace = call.get("namespace")
                if name == "read_record" and namespace == "fixture":
                    full_name = "fixture.read_record"
                elif name == "fixture.read_record":
                    full_name = name
                else:
                    raise ValueError("Tool outside the frozen fixture")
                args = json.loads(call.get("arguments", ""), object_pairs_hook=_pairs, parse_constant=_constant)
                canonical(args)
                if not isinstance(args, dict):
                    raise ValueError("Invalid tool arguments")
                output = execute_tool(task, full_name, args, tool_state)
                tool_trace.append({"namespace": "fixture", "name": "read_record", "arguments": args, "ok": output.get("ok") is True})
                results.append({"type": "function_call_output", "call_id": call_id, "output": json.dumps(output, ensure_ascii=True)})
            payload["input"].extend(replay + results)
        else:
            record["status"] = "tool_limit"
    except BudgetExhausted:
        record["status"] = "budget_exhausted" if db.has_attempt(outcome_key(row)) else "budget_stop"
    except TimeoutError:
        record["status"] = "task_deadline"
    except RouteConfigError as error:
        record["status"] = "unsupported_request"
        record["error_code"] = getattr(error, "code", "request_contract")
    except (ValueError, TypeError, KeyError, RecursionError):
        record["status"] = "invalid_task_response"
    finally:
        record["call_count"] = db.call_count(outcome_key(row))
        record["usage"] = summed_usage(envelopes) if record["call_count"] == len(envelopes) else None
        record["transport_completed_calls"] = sum(item.get("status") == "completed" for item in envelopes)
        record["latency_seconds"] = time.monotonic() - started
    record["answer"] = answer  # Only synthetic study answers, stored in protected local ledger.
    record["tool_trace"] = tool_trace
    if record["status"] != "completed":
        record["passed"], record["score"] = False, 0.0
    return record


async def run_window(config: dict, suite: dict, manifest: dict, db: StudyDB, stage: str, window: int) -> dict:
    prepared = reporting_manifest(manifest, db, stage)
    schedule = [row for row in prepared["schedule"] if row["window"] == window]
    if not schedule:
        raise ValueError("No frozen schedule for this stage/window; select a screened candidate first")
    if stage == "confirm" and window == 2:
        first = [row for row in prepared["schedule"] if row["window"] == 1]
        if any(db.outcome(outcome_key(row)) is None for row in first):
            raise ValueError("Complete the first frozen window before starting the second")
    comparisons = {item["id"]: item for item in config["comparisons"]}
    routes = {route["id"]: route for route in config["routes"]}
    tasks = {task["id"]: task for task in suite["tasks"]}
    processed = 0
    async with ResearchTransport(config["routes"], timeout_seconds=config["timeout_seconds"]) as transport:
        infos = {}
        for route_id in sorted({row["route_id"] for row in schedule}):
            infos[route_id] = await transport.preflight(route_id)
            pin_identity(db, routes[route_id], infos[route_id])
        unavailable = {}
        for comparison_id in {row["comparison_id"] for row in schedule}:
            pair = comparisons[comparison_id]
            if not all(infos[pair[arm]].get("available") for arm in ("baseline", "candidate")):
                unavailable[comparison_id] = "route_preflight_failed"
            elif not identity_matches(infos[pair["baseline"]], infos[pair["candidate"]]):
                unavailable[comparison_id] = "same_account_workspace_unverified"
            elif any(pair["model"] not in {model["id"] for model in infos[pair[arm]]["catalog"]["models"]}
                     for arm in ("baseline", "candidate")):
                unavailable[comparison_id] = "model_absent_from_current_catalog"
        db.window(stage, window, config["window_min_seconds"])
        for row in schedule:
            key = outcome_key(row)
            if db.outcome(key) is not None:
                continue
            if db.has_attempt(key):
                # A prior process may have died after submission. Charge remains;
                # never rerun the task and choose a more favorable answer.
                record = {**row, "status": "interrupted_unknown", "passed": False, "score": 0.0, "call_count": db.call_count(key),
                          "usage": None, "identity_verified": False, "model_match": False, "timestamp": now()}
            elif row["comparison_id"] in unavailable:
                record = {**row, "status": "preflight_unavailable", "error_code": unavailable[row["comparison_id"]],
                          "passed": False, "score": 0.0, "call_count": 0, "usage": None,
                          "identity_verified": False, "model_match": False, "timestamp": now()}
            else:
                record = await _one_task(tasks[row["task_id"]], row, comparisons[row["comparison_id"]], routes[row["route_id"]],
                                         transport, db, config, True)
            db.save_outcome(stage, key, record)
            processed += 1
            if record["status"] in {"budget_exhausted", "budget_stop"}:
                break
    return {"stage": stage, "window": window, "new_task_outcomes": processed,
            "total_reserved_calls": len(db.calls()), "quality_verified": False}
