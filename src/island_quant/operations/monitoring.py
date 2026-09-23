"""Persistent metrics, alerts, heartbeat and safe-mode state for paper operations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from island_quant.pipeline.artifacts import canonical_json, require_exact_version


class AlertLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertSink(Protocol):
    def send(self, alert: AlertRecord) -> None: ...


@dataclass(frozen=True, slots=True)
class AlertRecord:
    alert_id: str
    level: AlertLevel
    category: str
    message: str
    created_at: datetime
    status: str = "open"


@dataclass(frozen=True, slots=True)
class PaperRiskLimits:
    policy_version: str = "paper-risk-v1"
    maximum_daily_loss: Decimal = Decimal("50000")
    maximum_drawdown: Decimal = Decimal("0.10")
    maximum_gross_exposure: Decimal = Decimal("1")
    maximum_single_position: Decimal = Decimal("0.20")
    maximum_pending_order_age_seconds: int = 900
    maximum_reject_rate: Decimal = Decimal("0.25")
    maximum_data_age_seconds: int = 3600
    maximum_orders_per_session: int = 20
    maximum_notional_per_session: Decimal = Decimal("1000000")


REQUIRED_METRICS = (
    "service_health",
    "last_market_data_timestamp",
    "data_latency",
    "last_successful_decision",
    "oms_state_counts",
    "pending_order_age",
    "reject_count",
    "reject_rate",
    "fill_count",
    "partial_fills",
    "reconciliation_mismatches",
    "cash",
    "nav",
    "gross_exposure",
    "net_exposure",
    "daily_pnl",
    "drawdown",
    "scheduler_lag",
    "retry_count",
    "dead_letter_count",
    "safe_mode_status",
)

CRITICAL_ALERT_CATEGORIES = (
    "position_mismatch",
    "cash_mismatch",
    "unknown_broker_order",
    "market_data_stale",
    "daily_loss_limit",
    "drawdown_limit",
    "service_lost_heartbeat",
    "repeated_submit_failure",
    "database_corruption",
    "safe_mode_entered",
)


class FileAlertSink:
    def __init__(self, path: Path) -> None:
        self.path = path

    def send(self, alert: AlertRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(_alert_dict(alert), sort_keys=True) + "\n")


class OperationsStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS metrics(
                  name TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alerts(
                  alert_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,level TEXT NOT NULL,
                  category TEXT NOT NULL,message TEXT NOT NULL,created_at TEXT NOT NULL,
                  status TEXT NOT NULL,acknowledged_at TEXT,escalation INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS service_state(
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1),environment TEXT NOT NULL,
                  safe_mode INTEGER NOT NULL,reason TEXT,last_heartbeat TEXT,resume_reason TEXT
                );
                INSERT OR IGNORE INTO service_state VALUES(1,'paper',1,'not_started',NULL,NULL);
                CREATE TABLE IF NOT EXISTS service_starts(started_at TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS portfolio_snapshots(
                  projection_version TEXT PRIMARY KEY,as_of TEXT NOT NULL,
                  source_checksum TEXT NOT NULL,snapshot_checksum TEXT NOT NULL,
                  document TEXT NOT NULL,created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS current_portfolio(
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                  projection_version TEXT NOT NULL
                    REFERENCES portfolio_snapshots(projection_version)
                );
                CREATE TABLE IF NOT EXISTS target_snapshots(
                  artifact_version TEXT PRIMARY KEY,document_checksum TEXT NOT NULL,
                  document TEXT NOT NULL,created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS current_target(
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                  artifact_version TEXT NOT NULL REFERENCES target_snapshots(artifact_version)
                );
                """
            )
            for name in REQUIRED_METRICS:
                db.execute(
                    "INSERT OR IGNORE INTO metrics VALUES(?,'unavailable','not_recorded')",
                    (name,),
                )

    def metric(self, name: str, value: object, at: datetime) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO metrics VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET
                   value=excluded.value,updated_at=excluded.updated_at""",
                (name, str(value), at.isoformat()),
            )

    def publish_portfolio(
        self,
        projection_version: str,
        source_checksum: str,
        snapshot_checksum: str,
        document: dict[str, object],
        at: datetime,
    ) -> None:
        payload = json.dumps(document, sort_keys=True, separators=(",", ":"))
        with self._connect() as db:
            prior = db.execute(
                "SELECT document FROM portfolio_snapshots WHERE projection_version=?",
                (projection_version,),
            ).fetchone()
            if prior is not None and str(prior[0]) != payload:
                raise RuntimeError("paper portfolio projection version collision")
            db.execute(
                "INSERT OR IGNORE INTO portfolio_snapshots VALUES(?,?,?,?,?,?)",
                (
                    projection_version,
                    str(document["as_of"]),
                    source_checksum,
                    snapshot_checksum,
                    payload,
                    at.isoformat(),
                ),
            )
            db.execute(
                """INSERT INTO current_portfolio VALUES(1,?) ON CONFLICT(singleton)
                   DO UPDATE SET projection_version=excluded.projection_version""",
                (projection_version,),
            )
            for name in (
                "cash",
                "nav",
                "gross_exposure",
                "net_exposure",
                "daily_pnl",
                "drawdown",
            ):
                db.execute(
                    """INSERT INTO metrics VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET
                       value=excluded.value,updated_at=excluded.updated_at""",
                    (name, str(document[name]), at.isoformat()),
                )

    def current_portfolio(self) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT p.projection_version,p.document FROM portfolio_snapshots p
                   JOIN current_portfolio c
                   ON c.projection_version=p.projection_version WHERE c.singleton=1"""
            ).fetchone()
        if row is None:
            return None
        document = cast(dict[str, object], json.loads(str(row[1])))
        document["projection_version"] = str(row[0])
        return document

    def publish_target_snapshot(
        self, artifact_version: str, document: dict[str, object], at: datetime
    ) -> None:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("paper target projection timestamp must be timezone-aware")
        require_exact_version(artifact_version)
        if document.get("artifact_version") != artifact_version:
            raise RuntimeError("paper target projection version mismatch")
        payload = canonical_json(document).decode()
        checksum = hashlib.sha256(payload.encode()).hexdigest()
        with self._connect() as db:
            prior = db.execute(
                "SELECT document,document_checksum FROM target_snapshots WHERE artifact_version=?",
                (artifact_version,),
            ).fetchone()
            if prior is not None and (str(prior[0]) != payload or str(prior[1]) != checksum):
                raise RuntimeError("paper target projection version collision")
            db.execute(
                "INSERT OR IGNORE INTO target_snapshots VALUES(?,?,?,?)",
                (artifact_version, checksum, payload, at.isoformat()),
            )
            db.execute(
                """INSERT INTO current_target VALUES(1,?) ON CONFLICT(singleton)
                   DO UPDATE SET artifact_version=excluded.artifact_version""",
                (artifact_version,),
            )

    def current_target_snapshot(self) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT t.artifact_version,t.document_checksum,t.document
                   FROM target_snapshots t JOIN current_target c
                   ON c.artifact_version=t.artifact_version WHERE c.singleton=1"""
            ).fetchone()
        if row is None:
            return None
        document = cast(dict[str, object], json.loads(str(row[2])))
        checksum = hashlib.sha256(canonical_json(document)).hexdigest()
        if checksum != str(row[1]) or document.get("artifact_version") != str(row[0]):
            raise RuntimeError("paper target projection integrity check failed")
        return document

    def clear_current_target(self) -> None:
        """Deactivate target selection without deleting immutable target history."""
        with self._connect() as db:
            db.execute("DELETE FROM current_target WHERE singleton=1")

    def prior_portfolios(self, before: datetime) -> tuple[dict[str, object], ...]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT projection_version,document FROM portfolio_snapshots
                   WHERE as_of < ? ORDER BY as_of,projection_version""",
                (before.isoformat(),),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            document = cast(dict[str, object], json.loads(str(row[1])))
            document["projection_version"] = str(row[0])
            result.append(document)
        return tuple(result)

    def heartbeat(self, at: datetime) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE service_state SET last_heartbeat=? WHERE singleton=1", (at.isoformat(),)
            )

    def record_start(self, at: datetime, *, window: timedelta, maximum: int) -> bool:
        cutoff = (at - window).isoformat()
        with self._connect() as db:
            db.execute("DELETE FROM service_starts WHERE started_at < ?", (cutoff,))
            db.execute("INSERT OR IGNORE INTO service_starts VALUES(?)", (at.isoformat(),))
            count = int(db.execute("SELECT COUNT(*) FROM service_starts").fetchone()[0])
        return count <= maximum

    def enter_safe_mode(self, reason: str) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE service_state SET safe_mode=1,reason=?,resume_reason=NULL
                   WHERE singleton=1""",
                (reason,),
            )

    def resume(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("manual resume requires a reason")
        with self._connect() as db:
            db.execute(
                """UPDATE service_state SET safe_mode=0,reason=NULL,resume_reason=?
                   WHERE singleton=1""",
                (reason,),
            )

    def raise_alert(
        self,
        level: AlertLevel,
        category: str,
        message: str,
        at: datetime,
        *,
        cooldown: timedelta,
        sink: AlertSink | None = None,
    ) -> AlertRecord | None:
        fingerprint = hashlib.sha256(f"{level}:{category}:{message}".encode()).hexdigest()
        with self._connect() as db:
            prior = db.execute(
                """SELECT created_at,status,escalation FROM alerts WHERE fingerprint=?
                   ORDER BY created_at DESC LIMIT 1""",
                (fingerprint,),
            ).fetchone()
            if prior and prior[1] == "open" and at - datetime.fromisoformat(prior[0]) < cooldown:
                return None
            escalation = 0 if prior is None else int(prior[2]) + 1
            alert_id = hashlib.sha256(
                f"{fingerprint}:{at.isoformat()}:{escalation}".encode()
            ).hexdigest()
            alert = AlertRecord(alert_id, level, category, message, at)
            db.execute(
                "INSERT INTO alerts VALUES(?,?,?,?,?,?,'open',NULL,?)",
                (
                    alert_id,
                    fingerprint,
                    level.value,
                    category,
                    message,
                    at.isoformat(),
                    escalation,
                ),
            )
        if sink is not None:
            sink.send(alert)
        return alert

    def acknowledge(self, alert_id: str, at: datetime) -> None:
        with self._connect() as db:
            changed = db.execute(
                "UPDATE alerts SET status='acknowledged',acknowledged_at=? WHERE alert_id=?",
                (at.isoformat(), alert_id),
            )
        if changed.rowcount != 1:
            raise KeyError(alert_id)

    def snapshot(self, now: datetime, stale_after: timedelta) -> dict[str, object]:
        with self._connect() as db:
            state = dict(db.execute("SELECT * FROM service_state WHERE singleton=1").fetchone())
            metrics = {
                row[0]: {"value": row[1], "updated_at": row[2]}
                for row in db.execute("SELECT * FROM metrics")
            }
            alerts = [
                dict(row) for row in db.execute("SELECT * FROM alerts ORDER BY created_at DESC")
            ]
        heartbeat = state["last_heartbeat"]
        state["heartbeat_stale"] = (
            heartbeat is None or now - datetime.fromisoformat(heartbeat) > stale_after
        )
        return {
            "environment": "PAPER",
            "live_trading_enabled": False,
            "service": state,
            "metrics": metrics,
            "alerts": alerts,
        }

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


def evaluate_paper_risk(metrics: dict[str, Decimal], limits: PaperRiskLimits) -> tuple[str, ...]:
    failures: list[str] = []
    checks = (
        (metrics.get("daily_loss", Decimal("0")) > limits.maximum_daily_loss, "daily_loss_limit"),
        (metrics.get("drawdown", Decimal("0")) > limits.maximum_drawdown, "drawdown_limit"),
        (
            metrics.get("gross_exposure", Decimal("0")) > limits.maximum_gross_exposure,
            "gross_exposure_limit",
        ),
        (
            metrics.get("single_position", Decimal("0")) > limits.maximum_single_position,
            "single_position_limit",
        ),
        (
            metrics.get("reject_rate", Decimal("0")) > limits.maximum_reject_rate,
            "reject_rate_limit",
        ),
        (
            metrics.get("pending_order_age_seconds", Decimal("0"))
            > limits.maximum_pending_order_age_seconds,
            "pending_order_age_limit",
        ),
        (
            metrics.get("market_data_age_seconds", Decimal("0")) > limits.maximum_data_age_seconds,
            "market_data_stale",
        ),
        (
            metrics.get("orders", Decimal("0")) > limits.maximum_orders_per_session,
            "order_count_limit",
        ),
        (
            metrics.get("notional", Decimal("0")) > limits.maximum_notional_per_session,
            "notional_limit",
        ),
    )
    failures.extend(reason for failed, reason in checks if failed)
    return tuple(failures)


def enforce_paper_risk(
    state: OperationsStateStore,
    metrics: dict[str, Decimal],
    limits: PaperRiskLimits,
    at: datetime,
) -> tuple[str, ...]:
    failures = evaluate_paper_risk(metrics, limits)
    if failures:
        state.enter_safe_mode("paper_risk_limit")
        for reason in failures:
            level = (
                AlertLevel.CRITICAL if reason in CRITICAL_ALERT_CATEGORIES else AlertLevel.WARNING
            )
            state.raise_alert(
                level,
                reason,
                f"paper risk limit triggered: {reason}",
                at,
                cooldown=timedelta(minutes=30),
            )
    return failures


def _alert_dict(alert: AlertRecord) -> dict[str, str]:
    return {
        "alert_id": alert.alert_id,
        "level": alert.level.value,
        "category": alert.category,
        "message": alert.message,
        "created_at": alert.created_at.isoformat(),
        "status": alert.status,
    }
