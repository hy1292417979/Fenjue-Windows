"""Conservative, paired research reports; no network or application execution.

Every scheduled outcome is in the denominator, including upstream failures.
Missing outcomes block conclusions. Comparisons never pool account/workspace/
model cells. Confidence intervals resample complete task-family clusters, keeping
both arms and all windows together. Reports contain no prompts or raw answers.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import math
import random
import statistics
from typing import Any


_CATEGORIES = {"code", "constraints", "long_context", "tools"}
_SCREEN_TASKS = 12
_CONFIRM_TASKS = 32
_SCREEN_FAMILIES = 6
_CONFIRM_FAMILIES = 11
_UNATTEMPTED = {"budget_stop", "budget_exhausted", "unattempted", "not_attempted",
                "preflight_unavailable", "skipped", "pending"}
_IDENTITY_FIELDS = (
    "comparison_id", "pair_id", "task_id", "family", "category", "split",
    "window", "arm", "route_id",
)


def _category(value: Any) -> str:
    value = str(value)
    if value == "coding":
        return "code"
    return "long_context" if value in {"longcontext", "long-context"} else value


def _key(row: dict) -> tuple[str, ...]:
    return tuple(str(row.get(key, "")) for key in
                 ("comparison_id", "split", "window", "pair_id", "arm"))


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _attempted(row: dict | None) -> bool:
    if row is None:
        return False
    calls = _number(row.get("call_count"))
    if calls is not None and calls > 0:
        return True
    return row.get("status") not in _UNATTEMPTED


def _pass(row: dict | None) -> int:
    return int(bool(row and row.get("status") == "completed"
                    and row.get("passed") is True))


def _score(row: dict | None) -> float:
    if not row or row.get("status") != "completed":
        return 0.0
    score = _number(row.get("score"))
    return score if score is not None and 0 <= score <= 1 else 0.0


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _usage(rows: list[dict]) -> dict:
    """Unknown contributions propagate to totals; known subtotals stay labelled."""
    flattened = []
    names = {"input_tokens", "output_tokens", "total_tokens"}
    for row in rows:
        flat: dict[str, float | None] = {}

        def walk(value: Any, prefix: str = "") -> None:
            if not isinstance(value, dict):
                return
            for name, item in value.items():
                key = f"{prefix}.{name}" if prefix else str(name)
                if isinstance(item, dict):
                    walk(item, key)
                else:
                    number = _number(item)
                    flat[key] = number if number is not None and number >= 0 else None

        walk(row.get("usage"))
        names.update(flat)
        flattened.append(flat)
    counters = {}
    for name in sorted(names):
        known = [flat[name] for flat in flattened if flat.get(name) is not None]
        counters[name] = {
            "total": sum(known) if rows and len(known) == len(rows) else None,
            "known_subtotal": sum(known) if known else None,
            "known_rows": len(known),
            "unknown_rows": len(rows) - len(known),
        }
    return {"counters": counters, "actual_rows": len(rows), "cost_usd": None}


def _arm_metrics(expected: list[dict], actual: dict[tuple[str, ...], dict]) -> dict:
    rows = [actual[_key(item)] for item in expected if _key(item) in actual]
    attempted = [row for row in rows if _attempted(row)]
    passes = sum(_pass(actual.get(_key(item))) for item in expected)
    latency = [n for row in attempted if (n := _number(row.get("latency_seconds")))
               is not None and n >= 0]
    calls = [_number(row.get("call_count")) for row in rows]
    valid_calls = [int(n) for n in calls if n is not None and n >= 0 and n.is_integer()]
    transport = [_number(row.get("transport_completed_calls")) for row in rows]
    valid_transport = [int(n) for n, count in zip(transport, calls)
                       if n is not None and n >= 0 and n.is_integer()
                       and count is not None and n <= count]
    total_calls = sum(valid_calls) if len(valid_calls) == len(rows) else None
    completed_calls = sum(valid_transport) if rows and len(valid_transport) == len(rows) else None
    return {
        "scheduled": len(expected),
        "recorded": len(rows),
        "attempted": len(attempted),
        "missing": len(expected) - len(rows),
        "unattempted": len(rows) - len(attempted),
        "completed": sum(row.get("status") == "completed" for row in rows),
        "passed": passes,
        "pass_rate": passes / len(expected) if expected and attempted else None,
        "mean_score": (sum(_score(actual.get(_key(item))) for item in expected)
                       / len(expected) if expected and attempted else None),
        "denominator": "all_scheduled_outcomes",
        "partial": len(attempted) < len(expected),
        "status_counts": dict(sorted(Counter(str(row.get("status", "missing_status"))
                                             for row in rows).items())),
        "call_count": total_calls,
        "known_call_count": sum(valid_calls),
        "unknown_call_count_rows": len(rows) - len(valid_calls),
        "transport_completed_calls": completed_calls,
        "known_transport_completed_calls": sum(valid_transport) if valid_transport else None,
        "unknown_transport_count_rows": len(rows) - len(valid_transport),
        "transport_completion_rate": (completed_calls / total_calls
                                      if completed_calls is not None and total_calls else None),
        "latency_seconds": {
            "median": statistics.median(latency) if latency else None,
            "p95": _percentile(latency, .95),
            "known_rows": len(latency),
            "unknown_rows": len(attempted) - len(latency),
        },
        "usage": _usage(attempted),
    }


def _paired(expected: list[dict], actual: dict[tuple[str, ...], dict]) -> list[dict]:
    pairs: dict[tuple[str, str, str], dict[str, dict]] = defaultdict(dict)
    for item in expected:
        pairs[(str(item["split"]), str(item["window"]), str(item["pair_id"]))][item["arm"]] = item
    result = []
    for arms in pairs.values():
        if set(arms) != {"baseline", "candidate"}:
            continue
        baseline, candidate = arms["baseline"], arms["candidate"]
        b = _pass(actual.get(_key(baseline)))
        c = _pass(actual.get(_key(candidate)))
        result.append({"family": str(candidate["family"]),
                       "category": _category(candidate["category"]),
                       "window": str(candidate["window"]),
                       "task_id": str(candidate["task_id"]),
                       "baseline_attempted": _attempted(actual.get(_key(baseline))),
                       "candidate_attempted": _attempted(actual.get(_key(candidate))),
                       "baseline": b, "candidate": c, "difference": c - b})
    return result


def _bootstrap(pairs: list[dict], samples: int, seed: str) -> dict:
    clusters: dict[str, list[float]] = defaultdict(list)
    for pair in pairs:
        clusters[pair["family"]].append(pair["difference"])
    groups = [(sum(cluster), len(cluster)) for _, cluster in sorted(clusters.items())]
    estimate = sum(pair["difference"] for pair in pairs) / len(pairs) if pairs else None
    interval = None
    if len(groups) >= 2 and samples >= 100:
        rng = random.Random(int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16))
        draws = []
        for _ in range(samples):
            selected = rng.choices(groups, k=len(groups))
            draws.append(sum(group[0] for group in selected) / sum(group[1] for group in selected))
        interval = [_percentile(draws, .025), _percentile(draws, .975)]
    return {"method": "paired_family_cluster_percentile_bootstrap",
            "confidence_level": .95, "samples": samples,
            "family_clusters": len(groups), "paired_outcomes": len(pairs),
            "distinct_tasks": len({pair["task_id"] for pair in pairs}),
            "cluster_sizes": {family: len(cluster) for family, cluster in sorted(clusters.items())},
            "independence_assumption": "between_declared_task_families_not_verified",
            "interval_reliability": "limited_small_cluster_count" if len(groups) < 30 else "not_externally_validated",
            "gain": estimate, "ci95": interval,
            "candidate_wins": sum(pair["difference"] > 0 for pair in pairs),
            "baseline_wins": sum(pair["difference"] < 0 for pair in pairs),
            "ties": sum(pair["difference"] == 0 for pair in pairs)}


def _subset_metrics(pairs: list[dict]) -> dict:
    baseline_attempted = sum(pair["baseline_attempted"] for pair in pairs)
    candidate_attempted = sum(pair["candidate_attempted"] for pair in pairs)
    attempted_pairs = sum(pair["baseline_attempted"] and pair["candidate_attempted"] for pair in pairs)
    return {
        "paired_outcomes": len(pairs),
        "attempted_pairs": attempted_pairs,
        "family_clusters": len({pair["family"] for pair in pairs}),
        "distinct_tasks": len({pair["task_id"] for pair in pairs}),
        "baseline_attempted": baseline_attempted,
        "candidate_attempted": candidate_attempted,
        "baseline_rate": sum(pair["baseline"] for pair in pairs) / len(pairs) if baseline_attempted else None,
        "candidate_rate": sum(pair["candidate"] for pair in pairs) / len(pairs) if candidate_attempted else None,
        "gain": sum(pair["difference"] for pair in pairs) / len(pairs) if attempted_pairs else None,
        "denominator": "all_scheduled_pairs",
        "partial": attempted_pairs < len(pairs),
    }


def _window_starts(manifest: dict, comparison_id: str, rows: list[dict]) -> dict:
    starts = {}
    declared = manifest.get("window_started_at", {})
    if isinstance(declared, dict) and isinstance(declared.get(comparison_id), dict):
        declared = declared[comparison_id]
    if not isinstance(declared, dict):
        declared = {}
    for window in sorted({str(row.get("window")) for row in rows} | {"1", "2"}):
        timestamps = [timestamp for row in rows if str(row.get("window")) == window
                      and _attempted(row) and (timestamp := _timestamp(row.get("timestamp")))]
        observed = min(timestamps) if timestamps else None
        declared_start = _timestamp(declared.get(window, declared.get(int(window))
                                                  if window.isdigit() else None))
        starts[window] = {"observed": observed.isoformat() if observed else None,
                          "declared": declared_start.isoformat() if declared_start else None}
    return starts


def summarize(manifest: dict, rows: list[dict], *, bootstrap_samples: int = 5000) -> dict:
    """Summarize a frozen screen/confirm manifest without relaxing its gates.

    ``window_started_at`` is optional provenance. Spacing is always checked using
    actual attempted-row timestamps. Supplied ``score`` is descriptive; ``passed``
    on a completed first answer is the primary binary endpoint.
    """
    if not isinstance(manifest, dict) or not isinstance(rows, list):
        raise ValueError("manifest must be a dict and rows must be a list")
    if isinstance(bootstrap_samples, bool) or not isinstance(bootstrap_samples, int) or bootstrap_samples < 100:
        raise ValueError("bootstrap_samples must be an integer of at least 100")
    stage = manifest.get("stage", "screen")
    if stage not in {"screen", "confirm"}:
        raise ValueError("report stage must be screen or confirm")
    global_errors: list[str] = []
    warnings = ["No cross-account/workspace/model aggregate is a quality conclusion.",
                "This report does not establish production readiness or commercial eligibility."]
    for name in ("study_id", "suite_hash", "config_hash"):
        if not isinstance(manifest.get(name), str) or not manifest[name]:
            global_errors.append(f"missing_manifest_{name}")
    comparisons = manifest.get("comparisons", [])
    schedule = manifest.get("schedule", [])
    if not isinstance(comparisons, list) or not isinstance(schedule, list):
        raise ValueError("comparisons and schedule must be lists")
    by_id: dict[str, dict] = {}
    for comparison in comparisons:
        if not isinstance(comparison, dict) or not comparison.get("id"):
            global_errors.append("invalid_comparison")
            continue
        cid = str(comparison["id"])
        if cid in by_id:
            global_errors.append(f"duplicate_comparison:{cid}")
        by_id[cid] = comparison
    if not by_id:
        global_errors.append("no_comparisons")
    expected_all: dict[tuple[str, ...], dict] = {}
    errors: dict[str, list[str]] = defaultdict(list)
    for item in schedule:
        if not isinstance(item, dict) or any(item.get(field) in (None, "") for field in _IDENTITY_FIELDS):
            global_errors.append("invalid_schedule_row")
            continue
        cid = str(item["comparison_id"])
        if cid not in by_id:
            global_errors.append(f"unknown_schedule_comparison:{cid}")
            continue
        if item["arm"] not in {"baseline", "candidate"} or item["split"] not in {"screen", "confirm"}:
            errors[cid].append("invalid_schedule_arm_or_split")
            continue
        if str(item["route_id"]) != str(by_id[cid].get(item["arm"], "")):
            errors[cid].append("schedule_route_mismatch")
        key = _key(item)
        if key in expected_all:
            errors[cid].append("duplicate_schedule_row")
        else:
            expected_all[key] = item
    actual: dict[tuple[str, ...], dict] = {}
    duplicates: Counter[str] = Counter()
    unknown_rows = 0
    for row in rows:
        if not isinstance(row, dict):
            global_errors.append("invalid_result_row")
            continue
        key = _key(row)
        cid = str(row.get("comparison_id", ""))
        if key not in expected_all:
            unknown_rows += 1
            (errors[cid] if cid in by_id else global_errors).append("unknown_result_row")
            continue
        if key in actual:
            duplicates[cid] += 1
            errors[cid].append("duplicate_result_row")
            if any(actual[key].get(field) != row.get(field) for field in _IDENTITY_FIELDS):
                errors[cid].append("conflicting_duplicate_identity")
            continue  # Never select a better retry or a later answer.
        actual[key] = row
        expected = expected_all[key]
        for field in _IDENTITY_FIELDS:
            if str(expected.get(field)) != str(row.get(field)):
                errors[cid].append(f"result_schedule_mismatch:{field}")
    thresholds = manifest.get("thresholds", {})
    if not isinstance(thresholds, dict):
        thresholds = {}
        global_errors.append("invalid_thresholds")
    pass_threshold = _number(thresholds.get("pass_rate", .9))
    gain_threshold = _number(thresholds.get("min_gain", .1))
    min_gap = _number(manifest.get("window_min_seconds", 21600))
    if pass_threshold is None or not .9 <= pass_threshold <= 1:
        global_errors.append("invalid_or_relaxed_pass_threshold")
        pass_threshold = .9
    if gain_threshold is None or not .1 <= gain_threshold <= 1:
        global_errors.append("invalid_or_relaxed_gain_threshold")
        gain_threshold = .1
    if min_gap is None or min_gap < 21600:
        global_errors.append("invalid_or_relaxed_window_spacing")
        min_gap = 21600.0
    results = []
    for cid, comparison in by_id.items():
        expected = [item for item in expected_all.values()
                    if str(item["comparison_id"]) == cid and item["split"] == stage]
        recorded = [actual[_key(item)] for item in expected if _key(item) in actual]
        attempted = [row for row in recorded if _attempted(row)]
        issues = list(global_errors) + errors[cid]
        for field in ("baseline", "candidate", "account_ref", "workspace_ref", "model"):
            if not isinstance(comparison.get(field), str) or not comparison[field]:
                issues.append(f"missing_comparison_{field}")
        if comparison.get("baseline") == comparison.get("candidate"):
            issues.append("identical_route_ids")
        paired_schedule: dict[tuple[str, str], list[dict]] = defaultdict(list)
        seen_tasks: set[tuple[str, str, str]] = set()
        for item in expected:
            paired_schedule[(str(item["window"]), str(item["pair_id"]))].append(item)
            task_key = (str(item["window"]), str(item["task_id"]), str(item["arm"]))
            if task_key in seen_tasks:
                issues.append("duplicate_task_in_window")
            seen_tasks.add(task_key)
        for arms in paired_schedule.values():
            if len(arms) != 2 or {item["arm"] for item in arms} != {"baseline", "candidate"}:
                issues.append("unpaired_schedule")
            elif any(str(arms[0][field]) != str(arms[1][field])
                     for field in ("task_id", "family", "category")):
                issues.append("conflicting_pair_identity")
        for row in recorded:
            calls = _number(row.get("call_count"))
            if calls is None or calls < 0 or not calls.is_integer():
                issues.append("invalid_call_count")
            if not isinstance(row.get("status"), str) or not row["status"]:
                issues.append("missing_result_status")
            if row.get("status") == "completed":
                score = _number(row.get("score"))
                if not isinstance(row.get("passed"), bool) or score is None or not 0 <= score <= 1:
                    issues.append("invalid_completed_grade")
                if calls is None or calls < 1:
                    issues.append("completed_without_call")
            if _attempted(row):
                if row.get("identity_verified") is not True:
                    issues.append("identity_not_verified")
                completed_calls = _number(row.get("transport_completed_calls"))
                has_model_response = (row.get("status") == "completed"
                                      or bool(row.get("reported_model"))
                                      or (completed_calls is not None and completed_calls > 0))
                if (row.get("requested_model") != comparison.get("model")
                        or row.get("reported_model") not in (None, "", comparison.get("model"))
                        or row.get("model_mismatch") is True
                        or (has_model_response and row.get("model_match") is not True)
                        or (row.get("status") == "completed" and not row.get("reported_model"))):
                    issues.append("model_identity_indeterminate")
                if _timestamp(row.get("timestamp")) is None:
                    issues.append("invalid_or_missing_timestamp")
        missing = len(expected) - len(recorded)
        unattempted = len(recorded) - len(attempted)
        if not expected:
            issues.append("empty_schedule")
        if missing:
            issues.append("missing_scheduled_outcomes")
        if unattempted:
            issues.append("unattempted_scheduled_outcomes")
        pairs = _paired(expected, actual)
        stats = _bootstrap(pairs, bootstrap_samples,
                           f"{manifest.get('study_id')}:{manifest.get('suite_hash')}:{cid}:{stage}")
        stats["attempted_pairs"] = sum(pair["baseline_attempted"] and pair["candidate_attempted"] for pair in pairs)
        stats["coverage_complete"] = bool(expected) and not missing and not unattempted
        stats["denominator"] = "all_scheduled_pairs"
        if not stats["coverage_complete"]:
            for key in ("gain", "ci95", "candidate_wins", "baseline_wins", "ties"):
                stats[key] = None
        arms = {arm: _arm_metrics([item for item in expected if item["arm"] == arm], actual)
                for arm in ("baseline", "candidate")}
        categories = {}
        for category in sorted(_CATEGORIES | {_category(item["category"]) for item in expected}):
            subset = [pair for pair in pairs if pair["category"] == category]
            categories[category] = _subset_metrics(subset)
        if not _CATEGORIES <= {pair["category"] for pair in pairs}:
            issues.append("missing_required_category")
        if any(category not in _CATEGORIES for category in categories):
            issues.append("unknown_task_category")
        window_stats = {}
        for window in sorted({str(item["window"]) for item in expected}):
            subset = [pair for pair in pairs if pair["window"] == window]
            window_stats[window] = _subset_metrics(subset)
        starts = _window_starts(manifest, cid, recorded)
        first, second = (_timestamp(starts[window]["observed"]) for window in ("1", "2"))
        gap_seconds = (second - first).total_seconds() if first and second else None
        required_families = _CONFIRM_FAMILIES if stage == "confirm" else _SCREEN_FAMILIES
        required_tasks = _CONFIRM_TASKS if stage == "confirm" else _SCREEN_TASKS
        if stats["family_clusters"] < required_families:
            issues.append("insufficient_family_clusters")
        if stats["distinct_tasks"] < required_tasks:
            issues.append("insufficient_distinct_tasks")
        required_windows = ("1", "2") if stage == "confirm" else ("1",)
        if any(window_stats.get(window, {}).get("family_clusters", 0) < required_families
               for window in required_windows):
            issues.append("insufficient_family_clusters_in_each_window")
        if any(window_stats.get(window, {}).get("distinct_tasks", 0) < required_tasks
               for window in required_windows):
            issues.append("insufficient_distinct_tasks_in_each_window")
        if stage == "screen" and set(window_stats) != {"1"}:
            issues.append("screening_requires_window_one")
        if stage == "confirm":
            if set(window_stats) != {"1", "2"}:
                issues.append("confirmation_requires_two_windows")
            windows = [{(pair["family"], pair["task_id"], pair["category"])
                        for pair in pairs if pair["window"] == window} for window in ("1", "2")]
            if windows[0] != windows[1]:
                issues.append("confirmation_window_task_mismatch")
            if gap_seconds is None or gap_seconds < min_gap:
                issues.append("confirmation_window_spacing_not_met")
        candidate_rate = arms["candidate"]["pass_rate"]
        ci = stats["ci95"]
        gates = {
            "complete_valid_evidence": not issues,
            "candidate_pass_rate": candidate_rate is not None and candidate_rate >= pass_threshold,
            "minimum_gain": stats["gain"] is not None and stats["gain"] >= gain_threshold,
            "positive_ci_lower": ci is not None and ci[0] > 0,
            "both_windows_positive": all(window_stats.get(window, {}).get("gain") is not None
                                         and window_stats[window]["gain"] > 0 for window in ("1", "2"))
                                     if stage == "confirm" else None,
        }
        positive = all(value for value in gates.values() if value is not None)
        if issues:
            decision = "inconclusive"
        elif stage == "confirm":
            decision = "supporting_evidence" if positive else "not_met"
        else:
            decision = "promising" if positive else "no_signal"
        local_warnings = [f"category_regression:{category}" for category, values in categories.items()
                          if values["gain"] is not None and values["gain"] < 0]
        if stats["family_clusters"] < 30:
            local_warnings.append(
                "Few declared task-family clusters limit bootstrap interval reliability; "
                "variants and repeated windows add observations, not independent families. "
                "Between-family independence is an assumption and this interval is not a calibrated guarantee."
            )
        if any(row.get("status") != "completed" for row in attempted):
            local_warnings.append("Upstream and execution failures are scored zero.")
        if "model_identity_indeterminate" in issues:
            local_warnings.append("A reported-model or alias uncertainty is an identity gap, not evidence of lower quality.")
        results.append({
            "comparison_id": cid, "scope": {field: comparison.get(field) for field in
                ("baseline", "candidate", "account_ref", "workspace_ref", "model")},
            "stage": stage, "decision": decision, "production_ready": False,
            "complete": not issues, "candidate_pass_rate": candidate_rate,
            "gain": stats["gain"],
            "commercial_eligibility": "unresolved", "gates": gates,
            "issues": sorted(set(issues)), "warnings": local_warnings,
            "coverage": {"scheduled": len(expected), "recorded": len(recorded),
                         "missing": missing, "unattempted": unattempted,
                         "duplicate_rows": duplicates[cid], "required_families": required_families,
                         "required_distinct_tasks_per_window": required_tasks},
            "arms": arms, "paired": stats, "categories": categories,
            "windows": window_stats, "window_starts": starts,
            "observed_window_gap_seconds": gap_seconds,
        })
    return {
        "schema_version": 1, "study_id": manifest.get("study_id"),
        "suite_hash": manifest.get("suite_hash"), "config_hash": manifest.get("config_hash"),
        "stage": stage, "conclusion": "per_comparison_only",
        "production_ready": False, "commercial_eligibility": "unresolved",
        "thresholds": {"pass_rate": pass_threshold, "min_gain": gain_threshold,
                       "window_min_seconds": min_gap, "confirm_families": _CONFIRM_FAMILIES,
                       "screen_families": _SCREEN_FAMILIES,
                       "confirm_distinct_tasks_per_window": _CONFIRM_TASKS,
                       "screen_distinct_tasks_per_window": _SCREEN_TASKS},
        "comparisons": results, "global_issues": sorted(set(global_errors)),
        "unknown_result_rows": unknown_rows, "input_result_rows": len(rows),
        "decision_counts": dict(sorted(Counter(item["decision"] for item in results).items())),
        "usage": _usage([row for row in rows if isinstance(row, dict) and _attempted(row)]),
        "warnings": warnings,
        "interpretation": "First-answer endpoint; intended-outcome denominator; failed calls score zero; "
                          "missing or unattempted outcomes block conclusions; clusters are task families; "
                          "screening is exploratory and confirmation is scoped evidence only. "
                          "Zero observed attempts yield unknown rates; partial descriptive rates retain "
                          "the conservative scheduled denominator. Paired gain and CI require full coverage. "
                          "The pre-results budget revision fixes 12 screening tasks in 6 family clusters "
                          "and 32 confirmation tasks in 11 family clusters, so tool continuations fit "
                          "within the 300-call budget. Repeated tasks/windows do not increase cluster count; "
                          "few and uneven clusters limit confidence-interval reliability.",
    }


def render_markdown(report: dict) -> str:
    """Render the report's decisions, denominators, identity gates and limitations."""
    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    def rate(value: Any) -> str:
        return "unknown" if value is None else f"{value:.1%}"

    def scalar(value: Any) -> str:
        return "unknown" if value is None else cell(value)

    lines = [f"# Pathway research — {cell(report.get('study_id', 'unknown'))}", "",
             f"Stage: **{cell(report.get('stage'))}**. Decisions apply separately to each account/workspace/model comparison.",
             "", "Production readiness: **not established**. Commercial eligibility: **unresolved**.", "",
             f"Suite hash: `{cell(report.get('suite_hash'))}`  ",
             f"Configuration hash: `{cell(report.get('config_hash'))}`", "",
             cell(report.get("interpretation", "")), ""]
    if report.get("global_issues"):
        lines.extend(["Global evidence gaps: " + "; ".join(map(cell, report["global_issues"])), ""])
    if not report.get("comparisons"):
        lines.extend(["No comparison evidence is available. Conclusion: **inconclusive**.", ""])
    for comparison in report.get("comparisons", []):
        scope = comparison["scope"]
        lines.extend([f"## {cell(comparison['comparison_id'])}: {cell(comparison['decision'])}", "",
                      "Account/workspace/model: " + " / ".join(cell(scope.get(key)) for key in
                          ("account_ref", "workspace_ref", "model")), "",
                      f"Routes: {cell(scope.get('baseline'))} → {cell(scope.get('candidate'))}", "",
                      "| Arm | Scheduled | Attempted | Completed tasks | Passed | Pass rate | Calls | Completed calls | Transport completion | Median latency (s) |",
                      "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
        for arm, values in comparison["arms"].items():
            lines.append(f"| {arm} | {values['scheduled']} | {values['attempted']} | {values['completed']} | "
                         f"{values['passed']} | {rate(values['pass_rate'])} | {scalar(values['call_count'])} | "
                         f"{scalar(values['transport_completed_calls'])} | {rate(values['transport_completion_rate'])} | "
                         f"{scalar(values['latency_seconds']['median'])} |")
        lines.extend(["", "Completed calls count recorded terminal Responses completions, including tool turns. "
                      "Task pass rate measures final task correctness; missing transport counters remain unknown.", "",
                      "Task/family counts below describe the frozen schedule. Descriptive rates use the full scheduled "
                      "denominator when coverage is partial; no attempts means unknown. Paired gain and CI require complete coverage."])
        paired = comparison["paired"]
        ci = paired["ci95"]
        interval = f"[{rate(ci[0])}, {rate(ci[1])}]" if ci else "unknown"
        lines.extend(["", f"Paired gain: **{rate(paired['gain'])}**; 95% family-cluster bootstrap CI: **{interval}** "
                      f"({paired['distinct_tasks']} distinct tasks in {paired['family_clusters']} declared family clusters, "
                      f"{paired['samples']} resamples).", "",
                      f"Missing outcomes: {comparison['coverage']['missing']}; unattempted outcomes: "
                      f"{comparison['coverage']['unattempted']}; duplicate rows: {comparison['coverage']['duplicate_rows']}.", "",
                      "| Category | Paired outcomes | Distinct tasks | Family clusters | Baseline | Candidate | Gain |",
                      "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"])
        for category, values in comparison["categories"].items():
            lines.append(f"| {cell(category)} | {values['paired_outcomes']} | {values['distinct_tasks']} | {values['family_clusters']} | "
                         f"{rate(values['baseline_rate'])} | {rate(values['candidate_rate'])} | {rate(values['gain'])} |")
        lines.extend(["", "| Window | Distinct tasks | Family clusters | Candidate pass rate | Gain | Actual first attempt |",
                      "| --- | ---: | ---: | ---: | ---: | --- |"])
        for window, values in comparison["windows"].items():
            lines.append(f"| {cell(window)} | {values['distinct_tasks']} | {values['family_clusters']} | {rate(values['candidate_rate'])} | "
                         f"{rate(values['gain'])} | {cell(comparison['window_starts'].get(window, {}).get('observed'))} |")
        lines.extend(["", f"Observed window-start separation (seconds): {scalar(comparison['observed_window_gap_seconds'])}.", "",
                      "Gates: " + "; ".join(f"{cell(name)}={scalar(value)}" for name, value in comparison["gates"].items()), ""])
        if comparison["issues"]:
            lines.extend(["Evidence gaps: " + "; ".join(map(cell, comparison["issues"])), ""])
        for warning in comparison["warnings"]:
            lines.extend([f"- {cell(warning)}", ""])
        lines.extend(["| Arm | Usage counter | Total | Known subtotal | Unknown rows |",
                      "| --- | --- | ---: | ---: | ---: |"])
        for arm, values in comparison["arms"].items():
            for counter, usage in values["usage"]["counters"].items():
                lines.append(f"| {arm} | {cell(counter)} | {scalar(usage['total'])} | "
                             f"{scalar(usage['known_subtotal'])} | {usage['unknown_rows']} |")
        lines.extend(["", "Dollar cost: unknown; no token price or subscription-credit conversion has been assumed.", ""])
    lines.extend(["Candidate-specific evidence does not establish account-wide recovery or a distinct backend.", ""])
    return "\n".join(lines)
