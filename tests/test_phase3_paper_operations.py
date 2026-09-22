from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from island_quant.brokers.paper import DeterministicPaperBroker, PaperBrokerPolicy
from island_quant.dashboard.app import create_app
from island_quant.dashboard.operations import PaperOperationsQuery
from island_quant.dashboard.service import DashboardQueryService
from island_quant.oms.repository import SQLiteOMSRepository
from island_quant.operations.lifecycle import PaperServiceLifecycle
from island_quant.operations.monitoring import (
    AlertLevel,
    FileAlertSink,
    OperationsStateStore,
    PaperRiskLimits,
    enforce_paper_risk,
    evaluate_paper_risk,
)
from island_quant.operations.reports import PaperDailyReport, write_daily_report
from island_quant.operations.scheduler import FixedClock, JobDefinition, PaperScheduler, RunState

NOW = datetime(2025, 1, 2, 9, tzinfo=UTC)


def stores(tmp_path: Path) -> tuple[OperationsStateStore, PaperScheduler, SQLiteOMSRepository]:
    state = OperationsStateStore(tmp_path / "operations.sqlite")
    state.initialize()
    scheduler = PaperScheduler(tmp_path / "scheduler.sqlite", FixedClock(NOW))
    scheduler.initialize()
    repository = SQLiteOMSRepository(tmp_path / "oms.sqlite")
    repository.initialize()
    return state, scheduler, repository


def test_scheduler_idempotency_calendar_singleton_and_retry_dead_letter(tmp_path: Path) -> None:
    _, scheduler, _ = stores(tmp_path)
    calls: list[int] = []
    job = JobDefinition("decision_snapshot", "v1", maximum_attempts=2, backoff_seconds=0)
    assert scheduler.acquire_leader(timedelta(minutes=1))
    other = PaperScheduler(scheduler.path, FixedClock(NOW), owner="other")
    assert not other.acquire_leader(timedelta(minutes=1))
    assert (
        scheduler.run(
            job,
            session="2025-01-02",
            scheduled_at=NOW,
            trading_sessions=("2025-01-02",),
            handler=lambda: calls.append(1),
        )
        is RunState.COMPLETED
    )
    assert (
        scheduler.run(
            job,
            session="2025-01-02",
            scheduled_at=NOW,
            trading_sessions=("2025-01-02",),
            handler=lambda: calls.append(2),
        )
        is RunState.COMPLETED
    )
    assert calls == [1]
    failing = JobDefinition("reconciliation", "v1", maximum_attempts=2, backoff_seconds=0)

    def fail() -> None:
        raise RuntimeError("dependency outage")

    assert (
        scheduler.run(
            failing,
            session="2025-01-02",
            scheduled_at=NOW,
            trading_sessions=("2025-01-02",),
            handler=fail,
        )
        is RunState.RETRY
    )
    assert (
        scheduler.run(
            failing,
            session="2025-01-02",
            scheduled_at=NOW,
            trading_sessions=("2025-01-02",),
            handler=fail,
        )
        is RunState.DEAD_LETTER
    )
    with pytest.raises(ValueError, match="calendar"):
        scheduler.run(
            job,
            session="2025-01-03",
            scheduled_at=NOW,
            trading_sessions=("2025-01-02",),
            handler=lambda: None,
        )


def test_scheduler_misfire_clock_jump_and_timezone_guard(tmp_path: Path) -> None:
    _, scheduler, _ = stores(tmp_path)
    job = JobDefinition("daily_report", "v1", misfire_grace_seconds=1, catch_up=False)
    scheduler.clock = FixedClock(NOW + timedelta(hours=1))
    assert (
        scheduler.run(
            job,
            session="2025-01-02",
            scheduled_at=NOW,
            trading_sessions=("2025-01-02",),
            handler=lambda: None,
        )
        is RunState.DEAD_LETTER
    )
    scheduler.clock = FixedClock(datetime(2025, 1, 2, 9))
    with pytest.raises(ValueError, match="timezone"):
        scheduler.run(
            JobDefinition("x", "v1"),
            session="2025-01-02",
            scheduled_at=datetime(2025, 1, 2, 9),
            trading_sessions=("2025-01-02",),
            handler=lambda: None,
        )


def test_alert_dedup_ack_escalation_fixture_and_file_sink(tmp_path: Path) -> None:
    state, _, _ = stores(tmp_path)
    sink = FileAlertSink(tmp_path / "alerts.jsonl")
    first = state.raise_alert(
        AlertLevel.CRITICAL,
        "cash_mismatch",
        "cash differs",
        NOW,
        cooldown=timedelta(hours=1),
        sink=sink,
    )
    assert first is not None
    assert (
        state.raise_alert(
            AlertLevel.CRITICAL,
            "cash_mismatch",
            "cash differs",
            NOW,
            cooldown=timedelta(hours=1),
            sink=sink,
        )
        is None
    )
    state.acknowledge(first.alert_id, NOW)
    second = state.raise_alert(
        AlertLevel.CRITICAL,
        "cash_mismatch",
        "cash differs",
        NOW,
        cooldown=timedelta(hours=1),
        sink=sink,
    )
    assert second is not None
    assert len(sink.path.read_text().splitlines()) == 2


def test_lifecycle_restart_heartbeat_safe_mode_and_manual_resume(tmp_path: Path) -> None:
    state, _, repository = stores(tmp_path)
    lifecycle = PaperServiceLifecycle(
        repository, DeterministicPaperBroker((), PaperBrokerPolicy()), state
    )
    assert lifecycle.startup(NOW).ready
    assert lifecycle.health(NOW + timedelta(minutes=3)).reason == "stale_heartbeat"
    stopped = lifecycle.shutdown(NOW + timedelta(minutes=4), pending_work=True)
    assert stopped.safe_mode
    with pytest.raises(ValueError, match="reason"):
        state.resume("")
    state.resume("operator verified pending outbox")
    restarted = PaperServiceLifecycle(
        repository, DeterministicPaperBroker((), PaperBrokerPolicy()), state
    )
    assert restarted.startup(NOW + timedelta(minutes=5)).ready


def test_crash_loop_protection_enters_safe_mode(tmp_path: Path) -> None:
    state, _, repository = stores(tmp_path)
    broker = DeterministicPaperBroker((), PaperBrokerPolicy())
    for seconds in range(3):
        lifecycle = PaperServiceLifecycle(repository, broker, state)
        assert lifecycle.startup(NOW + timedelta(seconds=seconds)).live
    blocked = PaperServiceLifecycle(repository, broker, state).startup(NOW + timedelta(seconds=3))
    assert blocked.safe_mode
    assert blocked.reason == "crash_loop_detected"


def test_paper_risk_limits_trigger_kill_reasons() -> None:
    failures = evaluate_paper_risk(
        {
            "daily_loss": Decimal("50001"),
            "drawdown": Decimal("0.11"),
            "gross_exposure": Decimal("1.1"),
            "single_position": Decimal("0.3"),
            "reject_rate": Decimal("0.3"),
            "orders": Decimal("21"),
            "notional": Decimal("1000001"),
            "pending_order_age_seconds": Decimal("901"),
            "market_data_age_seconds": Decimal("3601"),
        },
        PaperRiskLimits(),
    )
    assert len(failures) == 9
    assert "daily_loss_limit" in failures
    assert "market_data_stale" in failures


def test_risk_failure_enters_kill_new_risk_without_liquidation(tmp_path: Path) -> None:
    state, _, repository = stores(tmp_path)
    failures = enforce_paper_risk(state, {"daily_loss": Decimal("50001")}, PaperRiskLimits(), NOW)
    assert failures == ("daily_loss_limit",)
    snapshot = state.snapshot(NOW, timedelta(minutes=1))
    service = snapshot["service"]
    assert isinstance(service, dict)
    assert service["safe_mode"] == 1
    assert repository.list_orders() == ()


def test_daily_report_is_pinned_idempotent_and_paper_only(tmp_path: Path) -> None:
    report = PaperDailyReport(
        1,
        "paper",
        "2025-01-02",
        NOW,
        "fresh",
        "strategy-v1",
        "model-v1",
        "a" * 64,
        1,
        1,
        0,
        (("2330", 100),),
        Decimal("900000"),
        Decimal("1000000"),
        Decimal("1000"),
        Decimal("10"),
        Decimal("0.5"),
        (),
        "passed",
        0,
        False,
        (),
    )
    version = write_daily_report(tmp_path, report)
    assert write_daily_report(tmp_path, report) == version
    assert (tmp_path / "paper-reports" / version / "report.json").exists()
    with pytest.raises(ValueError, match="paper"):
        replace(report, environment="live")


def test_disk_write_failure_is_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import island_quant.operations.reports as reports

    report = PaperDailyReport(
        1,
        "paper",
        "2025-01-02",
        NOW,
        "fresh",
        "s",
        "m",
        "a",
        0,
        0,
        0,
        (),
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
        (),
        "passed",
        0,
        False,
        (),
    )
    monkeypatch.setattr(reports.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError, match="disk"):
        write_daily_report(tmp_path, report)


def test_operations_dashboard_is_read_only_and_mode_isolated(tmp_path: Path) -> None:
    state, scheduler, repository = stores(tmp_path)
    state.heartbeat(NOW)
    query = PaperOperationsQuery(repository, state, scheduler)
    app = create_app(DashboardQueryService(operations_query=query))
    client = TestClient(app)
    root = client.get("/")
    assert root.status_code == 200
    assert "PAPER · READ ONLY · NOT LIVE" in root.text
    assert client.get("/api/dashboard/paper-operations").json()["environment"] == "PAPER"
    assert client.get("/health").json()["mode"] == "paper-read-only"
    assert client.post("/api/dashboard/paper-operations").status_code == 405
    assert client.get("/api/dashboard/overview").status_code == 404


def test_locked_operations_database_fails_visibly(tmp_path: Path) -> None:
    state, _, _ = stores(tmp_path)
    lock = sqlite3.connect(state.path)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            state.metric("service_health", "ok", NOW)
    finally:
        lock.rollback()
        lock.close()
