"""Calendar-aware, idempotent local scheduler primitives for paper operations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(slots=True)
class FixedClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


class RunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    RETRY = "retry"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True, slots=True)
class JobDefinition:
    job_id: str
    version: str
    maximum_attempts: int = 3
    backoff_seconds: int = 30
    misfire_grace_seconds: int = 900
    catch_up: bool = True


STANDARD_JOBS = tuple(
    JobDefinition(name, "paper-operations-v1")
    for name in (
        "data_freshness_check",
        "decision_snapshot",
        "feature_inference",
        "target_generation",
        "risk_evaluation",
        "paper_order_submission",
        "reconciliation",
        "mark_to_market",
        "daily_report",
        "health_heartbeat",
    )
)


class PaperScheduler:
    def __init__(self, path: Path, clock: Clock, *, owner: str = "paper-service") -> None:
        self.path = path
        self.clock = clock
        self.owner = owner

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS scheduler_runs(
                  run_key TEXT PRIMARY KEY, job_id TEXT NOT NULL, job_version TEXT NOT NULL,
                  session TEXT NOT NULL, scheduled_at TEXT NOT NULL, state TEXT NOT NULL,
                  attempts INTEGER NOT NULL, next_attempt_at TEXT, reason TEXT,
                  started_at TEXT, completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS scheduler_lock(
                  lock_name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at TEXT NOT NULL
                );
                """
            )

    def run(
        self,
        job: JobDefinition,
        *,
        session: str,
        scheduled_at: datetime,
        trading_sessions: Sequence[str],
        handler: Callable[[], None],
    ) -> RunState:
        now = self.clock.now()
        _aware_taipei(now)
        if session not in trading_sessions:
            raise ValueError("job session is not in pinned trading calendar")
        if now < scheduled_at:
            return RunState.PENDING
        lateness = (now - scheduled_at).total_seconds()
        run_key = f"{job.job_id}:{job.version}:{session}"
        with self._connect() as db:
            prior = db.execute(
                """SELECT state,attempts,next_attempt_at,started_at FROM scheduler_runs
                   WHERE run_key=?""",
                (run_key,),
            ).fetchone()
            if prior is not None and prior[0] == RunState.COMPLETED.value:
                return RunState.COMPLETED
            if (
                prior is not None
                and prior[0] == RunState.RUNNING.value
                and prior[3]
                and now - datetime.fromisoformat(str(prior[3]))
                <= timedelta(seconds=job.misfire_grace_seconds)
            ):
                return RunState.RUNNING
            if prior is None and lateness > job.misfire_grace_seconds and not job.catch_up:
                db.execute(
                    "INSERT INTO scheduler_runs VALUES(?,?,?,?,?,'dead_letter',1,NULL,?,?,NULL)",
                    (
                        run_key,
                        job.job_id,
                        job.version,
                        session,
                        scheduled_at.isoformat(),
                        "misfire_outside_grace",
                        now.isoformat(),
                    ),
                )
                return RunState.DEAD_LETTER
            if prior is not None and prior[2] and now < datetime.fromisoformat(str(prior[2])):
                return RunState.RETRY
            attempts = 1 if prior is None else int(prior[1]) + 1
            db.execute(
                """INSERT INTO scheduler_runs VALUES(?,?,?,?,?,'running',?,NULL,NULL,?,NULL)
                   ON CONFLICT(run_key) DO UPDATE SET state='running',attempts=excluded.attempts,
                   started_at=excluded.started_at""",
                (
                    run_key,
                    job.job_id,
                    job.version,
                    session,
                    scheduled_at.isoformat(),
                    attempts,
                    now.isoformat(),
                ),
            )
        try:
            handler()
        except Exception as exc:
            state = RunState.DEAD_LETTER if attempts >= job.maximum_attempts else RunState.RETRY
            next_at = now + timedelta(seconds=job.backoff_seconds * attempts)
            with self._connect() as db:
                db.execute(
                    "UPDATE scheduler_runs SET state=?,next_attempt_at=?,reason=? WHERE run_key=?",
                    (state.value, next_at.isoformat(), type(exc).__name__, run_key),
                )
            return state
        with self._connect() as db:
            db.execute(
                "UPDATE scheduler_runs SET state='completed',completed_at=? WHERE run_key=?",
                (now.isoformat(), run_key),
            )
        return RunState.COMPLETED

    def acquire_leader(self, ttl: timedelta) -> bool:
        now = self.clock.now()
        expires = now + ttl
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT owner,expires_at FROM scheduler_lock WHERE lock_name='leader'"
            ).fetchone()
            if (
                row is not None
                and datetime.fromisoformat(str(row[1])) > now
                and row[0] != self.owner
            ):
                return False
            db.execute(
                """INSERT INTO scheduler_lock VALUES('leader',?,?)
                   ON CONFLICT(lock_name) DO UPDATE SET owner=excluded.owner,
                   expires_at=excluded.expires_at""",
                (self.owner, expires.isoformat()),
            )
        return True

    def runs(self) -> tuple[dict[str, object], ...]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM scheduler_runs ORDER BY scheduled_at,job_id"
            ).fetchall()
        return tuple(dict(row) for row in rows)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


def _aware_taipei(value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError("scheduler clock must be timezone-aware")
    value.astimezone(ZoneInfo("Asia/Taipei"))
