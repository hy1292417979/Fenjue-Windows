"""Portable research CLI. Offline commands never load tokens or open the network."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys

from .config import digest
from .report import summarize, render_markdown
from .storage import now, replace_json
from .study import prepare, open_study, reporting_manifest


def _emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2))


def _stamp(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Window timestamps require an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _report(manifest: dict, db, stage: str) -> dict:
    result = summarize(reporting_manifest(manifest, db, stage), db.rows(stage))
    calls = db.calls()
    result["call_ledger"] = {
        "reserved_total": len(calls),
        "reserved_by_stage": {name: sum(call["stage"] == name for call in calls)
                              for name in ("eligibility", "screen", "confirm")},
        "unfinished_reservations": sum(call["finished_at"] is None for call in calls),
        "caps": manifest["budget"],
    }
    result["confirmation_selection"] = db.metadata("confirm_selection")
    return result


def _write_report(directory: Path, report: dict, stage: str) -> dict:
    replace_json(directory / (stage + "-report.json"), report)
    path = directory / (stage + "-report.md")
    temporary = path.with_suffix(".md.pending")
    with temporary.open("x", encoding="utf-8") as target:
        target.write(render_markdown(report))
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)
    return {"report": str(path), "stage": stage, "decision_counts": report["decision_counts"],
            "inference_calls": report["call_ledger"]["reserved_total"], "production_ready": False}


def _select(directory: Path, manifest: dict, db, args) -> dict:
    previous = db.metadata("confirm_selection")
    if previous is not None:
        return {"selection": previous, "already_frozen": True}
    report = _report(manifest, db, "screen")
    _write_report(directory, report, "screen")
    eligible = [item for item in report["comparisons"] if item["complete"] and item["decision"] == "promising"]
    if not eligible:
        return {"selection": None, "status": "no_eligible_screening_signal", "production_ready": False}
    winner = sorted(eligible, key=lambda item: (-item["gain"], -item["candidate_pass_rate"], item["comparison_id"]))[0]
    first = _stamp(args.window_one)
    second = _stamp(args.window_two) if args.window_two else first + timedelta(seconds=manifest["window_min_seconds"] + 7200)
    if (second - first).total_seconds() < manifest["window_min_seconds"]:
        raise ValueError("The preselected windows must be at least the frozen interval apart")
    if first < datetime.now(timezone.utc) - timedelta(minutes=1):
        raise ValueError("Select future windows before opening confirmation results")
    selection = {
        "comparison_id": winner["comparison_id"], "selected_at": now(),
        "rule": "complete_promising_then_gain_then_pass_rate_then_comparison_id",
        "screen_report_hash": digest(report),
        "windows": {"1": first.isoformat(), "2": second.isoformat()},
        "start_tolerance_seconds": 7200,
    }
    db.set_metadata_once("confirm_selection", selection)
    return {"selection": selection, "already_frozen": False, "production_ready": False}


def _check_window(db, window: int) -> None:
    selection = db.metadata("confirm_selection")
    if not selection:
        raise ValueError("Run select after completed screening before confirmation")
    # A started window may resume; reserved/returned answers are never repeated.
    if str(window) in db.window_times("confirm"):
        return
    start = _stamp(selection["windows"][str(window)])
    current = datetime.now(timezone.utc)
    if current < start:
        raise ValueError("The frozen confirmation window has not opened yet")
    if current > start + timedelta(seconds=selection["start_tolerance_seconds"]):
        raise ValueError("The frozen window start was missed; keep results as inconclusive")


def _diff(config: dict, suite: dict, comparison_id: str | None) -> dict:
    from .routes import ENDPOINTS, RouteConfigError, adapt_payload
    from .tasks import initial_payload

    comparisons = [item for item in config["comparisons"] if comparison_id is None or item["id"] == comparison_id]
    if not comparisons:
        raise ValueError("No configured comparison matched the requested identifier")
    routes = {item["id"]: item for item in config["routes"]}
    samples = {}
    for task in suite["tasks"]:
        if task["split"] == "screen":
            samples.setdefault(task["category"], task)
    result = []
    for comparison in comparisons:
        cases = []
        for category, task in samples.items():
            arms = {}
            for arm in ("baseline", "candidate"):
                route = routes[comparison[arm]]
                payload = initial_payload(task, comparison["model"], comparison["reasoning"])
                if task.get("tools") and route["kind"] != "bps":
                    payload["include"] = ["reasoning.encrypted_content"]
                try:
                    outgoing = adapt_payload(route, payload)
                    arms[arm] = {"route_id": route["id"], "endpoint": ENDPOINTS[route["kind"]],
                                 "request": outgoing, "request_hash": digest(outgoing)}
                except RouteConfigError as error:
                    arms[arm] = {"route_id": route["id"], "status": "unsupported_request", "error_code": error.code}
            cases.append({"task_id": task["id"], "category": category, "arms": arms})
        result.append({"comparison_id": comparison["id"], "cases": cases})
    return {"comparisons": result, "inference_calls": 0, "credentials_loaded": False,
            "note": "Synthetic public prompts only. Transport may add per-task BPS metadata; see adapter evidence."}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Private same-account pathway study; no automatic retries or account switching")
    commands = root.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare", help="Offline: freeze config, hidden graders and schedule")
    prepare_parser.add_argument("--config", required=True, type=Path)
    prepare_parser.add_argument("--out", required=True, type=Path)
    for name, help_text in (
        ("inventory", "Network GET only: verify identity and inspect available catalogs"),
        ("eligibility", "Charge a first-response readiness call per route/model"),
        ("screen", "Execute the frozen first-answer paired screening schedule"),
        ("select", "Offline: automatically freeze a qualifying candidate and two windows"),
        ("confirm", "Execute one previously selected confirmation window"),
        ("report", "Offline: export per-comparison evidence and budget accounting"),
        ("request-diff", "Offline: show actual adapter transformations of public synthetic tasks"),
    ):
        item = commands.add_parser(name, help=help_text)
        item.add_argument("--study", required=True, type=Path)
        if name == "confirm":
            item.add_argument("--window", required=True, type=int, choices=(1, 2))
        if name == "report":
            item.add_argument("--stage", choices=("screen", "confirm"), default="screen")
        if name == "select":
            item.add_argument("--window-one", help="ISO timestamp with timezone; default now")
            item.add_argument("--window-two", help="ISO timestamp with timezone; default first + frozen interval + 2h start tolerance")
        if name == "request-diff":
            item.add_argument("--comparison")
    return root


def main() -> int:
    args = parser().parse_args()
    db = None
    try:
        if args.command == "prepare":
            _emit(prepare(args.config.resolve(), args.out.resolve()))
            return 0
        directory = args.study.resolve()
        config, suite, manifest, db = open_study(directory)
        with db.exclusive_run():
            if args.command == "report":
                result = _write_report(directory, _report(manifest, db, args.stage), args.stage)
            elif args.command == "request-diff":
                result = _diff(config, suite, args.comparison)
                path = directory / "request-diff.json"
                replace_json(path, result)
                result = {"request_diff": str(path), "inference_calls": 0, "credentials_loaded": False}
            elif args.command == "select":
                result = _select(directory, manifest, db, args)
            else:
                from .runner import inspect_routes, run_window
                if args.command in ("inventory", "eligibility"):
                    result = asyncio.run(inspect_routes(config, db, call_readiness=args.command == "eligibility"))
                    replace_json(directory / (args.command + ".json"), result)
                else:
                    window = args.window if args.command == "confirm" else 1
                    if args.command == "confirm":
                        _check_window(db, window)
                    result = asyncio.run(run_window(config, suite, manifest, db, args.command, window))
                    result["report"] = _write_report(directory, _report(manifest, db, args.command), args.command)["report"]
            _emit(result)
        return 0
    except KeyboardInterrupt:
        _emit({"status": "interrupted", "message": "Keep the ledger; submitted calls remain charged and are not replayed"})
        return 130
    except (ValueError, OSError) as error:
        message = str(error) if isinstance(error, ValueError) else "File or permission operation failed"
        _emit({"status": "error", "code": getattr(error, "code", "study_config_or_state_invalid"), "message": message})
        return 2
    except Exception:
        _emit({"status": "error", "code": "unexpected_local_failure", "message": "Preserve the study and ledger; raw exception data is intentionally omitted"})
        return 3
    finally:
        if db is not None:
            db.close()


if __name__ == "__main__":
    sys.exit(main())
