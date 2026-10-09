"""Freeze study material and balanced schedules before observing any answers."""

from __future__ import annotations

import random
from pathlib import Path

from .config import canonical, digest, load_config, read_json
from .storage import StudyDB, now, secure_directory, write_json_new
from .tasks import build_suite


def schedule(config: dict, suite: dict, stage: str, comparison_ids: list[str] | None = None) -> list[dict]:
    tasks = [task for task in suite["tasks"] if task["split"] == stage]
    rng = random.Random(f"{config['seed']}:{stage}")
    rng.shuffle(tasks)
    comparisons = [item for item in config["comparisons"] if comparison_ids is None or item["id"] in comparison_ids]
    rows = []
    for window in ((1, 2) if stage == "confirm" else (1,)):
        first_arms = {}
        for comparison in comparisons:
            choices = [index % 2 == 0 for index in range(len(tasks))]
            rng.shuffle(choices)
            first_arms[comparison["id"]] = choices
        # Every pair stays adjacent, comparisons alternate within a task block.
        for index, task in enumerate(tasks):
            for offset, comparison in enumerate(comparisons):
                arms = ["baseline", "candidate"]
                if first_arms[comparison["id"]][index]:
                    arms.reverse()
                pair_id = f"{stage}:{comparison['id']}:{task['id']}:w{window}"
                for arm in arms:
                    rows.append({
                        "pair_id": pair_id, "comparison_id": comparison["id"], "task_id": task["id"],
                        "family": task["family"], "category": task["category"], "split": stage,
                        "window": window, "arm": arm, "route_id": comparison[arm],
                    })
    return rows


def prepare(config_path: Path, directory: Path) -> dict:
    config = load_config(config_path)
    if directory.exists():
        raise ValueError("Prepare requires a new study directory; existing budgets and results are never reset")
    secure_directory(directory)
    suite = build_suite(config["seed"])
    stage_calls = {stage: sum(task["max_turns"] for task in suite["tasks"] if task["split"] == stage)
                   for stage in ("screen", "confirm")}
    manifest = {
        "version": 1, "study_id": config["study_id"], "created_at": now(),
        "config_hash": digest(config), "suite_hash": digest(suite),
        "thresholds": {"pass_rate": 0.9, "min_gain": 0.1},
        "window_min_seconds": config["window_min_seconds"], "budget": config["budget"],
        "comparisons": config["comparisons"],
        "planned_call_ceiling": {"eligibility": config["budget"]["eligibility"],
                                 "screen": stage_calls["screen"] * 2 * len(config["comparisons"]),
                                 "confirm": stage_calls["confirm"] * 4},
        "screen_schedule": schedule(config, suite, "screen"),
        "confirm_schedules": {item["id"]: schedule(config, suite, "confirm", [item["id"]]) for item in config["comparisons"]},
        "quality_verified": False, "production_ready": False,
    }
    write_json_new(directory / "config.json", config)
    write_json_new(directory / "suite.json", suite)
    write_json_new(directory / "manifest.json", manifest)
    write_json_new(directory / "seal.json", {"manifest_hash": digest(manifest)})
    db = StudyDB(directory, config["budget"])
    db.set_metadata_once("study_seal", digest(manifest))
    db.close()
    return {"study_id": config["study_id"], "task_count": len(suite["tasks"]), "screen_task_attempts": len(manifest["screen_schedule"]),
            "config_hash": manifest["config_hash"], "suite_hash": manifest["suite_hash"], "inference_calls": 0}


def open_study(directory: Path) -> tuple[dict, dict, dict, StudyDB]:
    config = read_json(directory / "config.json")
    suite = read_json(directory / "suite.json")
    manifest = read_json(directory / "manifest.json")
    seal = read_json(directory / "seal.json")
    if digest(config) != manifest.get("config_hash") or digest(suite) != manifest.get("suite_hash") or digest(manifest) != seal.get("manifest_hash"):
        raise ValueError("Frozen study material changed; resume was stopped")
    db = StudyDB(directory, config["budget"])
    if db.metadata("study_seal") != seal["manifest_hash"]:
        db.close()
        raise ValueError("Ledger does not match this study's seal")
    return config, suite, manifest, db


def reporting_manifest(manifest: dict, db: StudyDB, stage: str) -> dict:
    if stage == "confirm":
        selected = db.metadata("confirm_selection")
        comparisons = [item for item in manifest["comparisons"] if selected and item["id"] == selected["comparison_id"]]
        selected_schedule = manifest["confirm_schedules"].get(selected["comparison_id"], []) if selected else []
    else:
        comparisons = manifest["comparisons"]
        selected_schedule = manifest["screen_schedule"]
    return {**manifest, "stage": stage, "comparisons": comparisons, "schedule": selected_schedule,
            "window_started_at": db.window_times(stage)}
