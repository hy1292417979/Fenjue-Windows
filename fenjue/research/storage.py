"""Private local study storage with crash-conservative, transactionally charged calls."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import canonical, read_json


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def secure_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    if path.is_symlink():
        raise ValueError("Study directory must not be a symlink")
    if os.name == "nt":
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, check=True)
        # Only SID bytes are needed; the localized account name can use a
        # different Windows console encoding from Python's text-mode default.
        match = re.search(rb',"(S-1-[0-9-]+)"\s*$', result.stdout.strip())
        if match is None:
            raise ValueError("Current Windows identity is unavailable")
        sid = match.group(1).decode("ascii")
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"], capture_output=True, check=True)
    else:
        path.chmod(0o700)


def write_json_new(path: Path, value) -> None:
    raw = canonical(value) + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())


def replace_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + ".pending")
    write_json_new(temporary, value)
    os.replace(temporary, path)


class BudgetExhausted(Exception):
    pass


class StudyDB:
    def __init__(self, directory: Path, budget: dict):
        self.directory = directory
        self.budget = budget
        self.connection = sqlite3.connect(directory / "ledger.sqlite3", timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY, stage TEXT NOT NULL, route_id TEXT NOT NULL,
                task_key TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT, record TEXT
            );
            CREATE TABLE IF NOT EXISTS outcomes (key TEXT PRIMARY KEY, stage TEXT NOT NULL, record TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS windows (stage TEXT NOT NULL, window INTEGER NOT NULL, started_at TEXT NOT NULL, PRIMARY KEY(stage, window));
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self.connection.commit()

    def close(self):
        self.connection.close()

    @contextmanager
    def exclusive_run(self):
        lock = self.directory / "run.lock"
        try:
            write_json_new(lock, {"pid": os.getpid(), "started_at": now()})
        except FileExistsError:
            raise ValueError("A run lock exists. Confirm that its process stopped before removing this study's run.lock.") from None
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)

    def charge(self, stage: str, route_id: str, task_key: str) -> int:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            total = self.connection.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            count = self.connection.execute("SELECT COUNT(*) FROM calls WHERE stage=?", (stage,)).fetchone()[0]
            if total >= self.budget["total"] or count >= self.budget[stage]:
                raise BudgetExhausted("Study or stage call budget exhausted")
            row = self.connection.execute("INSERT INTO calls(stage,route_id,task_key,started_at) VALUES(?,?,?,?)", (stage, route_id, task_key, now()))
            call_id = row.lastrowid
            self.connection.commit()
            return call_id
        except BaseException:
            self.connection.rollback()
            raise

    def finish_call(self, call_id: int, record: dict) -> None:
        self.connection.execute("UPDATE calls SET finished_at=?,record=? WHERE id=? AND record IS NULL", (now(), canonical(record).decode(), call_id))
        self.connection.commit()

    def has_attempt(self, task_key: str) -> bool:
        return self.connection.execute("SELECT 1 FROM calls WHERE task_key=? LIMIT 1", (task_key,)).fetchone() is not None

    def call_count(self, task_key: str) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM calls WHERE task_key=?", (task_key,)).fetchone()[0]

    def save_outcome(self, stage: str, key: str, record: dict) -> None:
        self.connection.execute("INSERT INTO outcomes(key,stage,record) VALUES(?,?,?)", (key, stage, canonical(record).decode()))
        self.connection.commit()

    def outcome(self, key: str):
        row = self.connection.execute("SELECT record FROM outcomes WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def rows(self, stage: str) -> list[dict]:
        return [json.loads(row[0]) for row in self.connection.execute("SELECT record FROM outcomes WHERE stage=? ORDER BY rowid", (stage,))]

    def calls(self) -> list[dict]:
        return [dict(row) | {"record": json.loads(row["record"]) if row["record"] else None} for row in self.connection.execute("SELECT * FROM calls ORDER BY id")]

    def window(self, stage: str, number: int, separation: int) -> str:
        row = self.connection.execute("SELECT started_at FROM windows WHERE stage=? AND window=?", (stage, number)).fetchone()
        if row:
            return row[0]
        if stage == "confirm" and number == 2:
            first = self.connection.execute("SELECT started_at FROM windows WHERE stage='confirm' AND window=1").fetchone()
            if not first or time.time() - datetime.fromisoformat(first[0]).timestamp() < separation:
                raise ValueError("Second confirmation window requires the frozen interval after window one")
        value = now()
        self.connection.execute("INSERT INTO windows(stage,window,started_at) VALUES(?,?,?)", (stage, number, value))
        self.connection.commit()
        return value

    def window_times(self, stage: str) -> dict:
        return {str(row[0]): row[1] for row in self.connection.execute("SELECT window,started_at FROM windows WHERE stage=?", (stage,))}

    def metadata(self, key: str):
        row = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_metadata_once(self, key: str, value) -> None:
        self.connection.execute("INSERT INTO metadata(key,value) VALUES(?,?)", (key, canonical(value).decode()))
        self.connection.commit()
