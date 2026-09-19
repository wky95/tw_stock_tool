from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from island_quant.brokers.paper import (
    DeterministicPaperBroker,
    PaperBrokerPolicy,
    PaperMarketEvent,
    PaperOrderRequest,
)
from island_quant.oms.models import (
    ALLOWED_TRANSITIONS,
    OMSEvent,
    OMSInvariantError,
    OMSState,
    validate_transition,
)
from island_quant.oms.reconciliation import reconcile_oms
from island_quant.oms.repository import OMSPersistenceError, SQLiteOMSRepository
from island_quant.oms.service import PaperOMSService, PaperOrderCommand

NOW = datetime(2025, 1, 2, 9, tzinfo=UTC)


def repo(tmp_path: Path) -> SQLiteOMSRepository:
    value = SQLiteOMSRepository(tmp_path / "state" / "paper.sqlite", timeout=0.01)
    value.initialize()
    return value


def command(key: str = "key", *, cash: str = "100000") -> PaperOrderCommand:
    return PaperOrderCommand("client-1", key, "2330", "buy", 100, Decimal("10"), Decimal(cash), NOW)


def broker(*, volume: int = 500, reject: bool = False) -> DeterministicPaperBroker:
    market = (PaperMarketEvent("2330", NOW, Decimal("10"), volume),)
    return DeterministicPaperBroker(market, PaperBrokerPolicy(reject_all=reject))


def test_all_declared_transitions_and_all_other_edges_fail_closed() -> None:
    for previous in OMSState:
        for following in OMSState:
            if following in ALLOWED_TRANSITIONS[previous]:
                validate_transition(previous, following)
            else:
                with pytest.raises(OMSInvariantError):
                    validate_transition(previous, following)


def test_duplicate_command_and_delivery_are_idempotent(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    service = PaperOMSService(repository)
    first = service.queue(command())
    assert service.queue(command()) == first
    paper = broker()
    assert service.process_outbox(paper) == 1
    assert service.process_outbox(paper) == 0
    order = repository.get(first.order_id)
    assert order.state is OMSState.PARTIALLY_FILLED
    assert order.filled_quantity == 50
    assert len(repository.fills()) == 1


def test_commit_then_restart_and_crash_after_broker_success_recovers(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    queued = PaperOMSService(repository).queue(command())
    restarted = SQLiteOMSRepository(repository.path)
    restarted.initialize()
    service = PaperOMSService(restarted)
    paper = broker(volume=2000)
    with pytest.raises(RuntimeError, match="injected crash"):
        service.process_outbox(paper, crash_after_broker_success=True)
    assert restarted.get(queued.order_id).state is OMSState.SUBMIT_PENDING
    assert service.process_outbox(paper) == 1
    assert restarted.get(queued.order_id).state is OMSState.FILLED
    assert len(restarted.fills()) == 1


def test_paper_broker_is_deterministic_partial_and_no_fill_conditions() -> None:
    request = PaperOrderRequest("c", "k", "2330", "buy", 100)
    paper = broker()
    assert paper.submit(request) == paper.submit(request)
    assert paper.submit(request).fills[0].quantity == 50
    for event in (
        PaperMarketEvent("2330", NOW, Decimal("10"), 0),
        PaperMarketEvent("2330", NOW, Decimal("10"), 100, suspended=True),
        PaperMarketEvent("2330", NOW, Decimal("10"), 100, limit_locked=True),
    ):
        result = DeterministicPaperBroker((event,), PaperBrokerPolicy()).submit(request)
        assert not result.fills
    missing = DeterministicPaperBroker((), PaperBrokerPolicy()).submit(request)
    assert missing.acknowledged_at.year == 1970


def test_risk_reject_and_paper_environment_isolation(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    rejected = PaperOMSService(repository).queue(command(cash="1"))
    assert rejected.state is OMSState.RISK_REJECTED
    with pytest.raises(ValueError, match="paper"):
        SQLiteOMSRepository(tmp_path / "live.sqlite", environment="live")


def test_illegal_out_of_order_sequence_gap_and_corrupted_checksum(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    order = PaperOMSService(repository).queue(command())
    bad = OMSEvent.create(
        order_id=order.order_id,
        client_order_id=order.client_order_id,
        idempotency_key=order.idempotency_key,
        correlation_id=order.order_id,
        causation_id=None,
        expected_previous_state=order.state,
        state=OMSState.FILLED,
        event_time=NOW,
        received_time=NOW,
        sequence=order.sequence + 2,
        reason_code="bad",
        business_identity="bad",
    )
    with pytest.raises(OMSInvariantError, match="sequence"):
        repository.transition(order.order_id, bad, expected_version=order.version)
    with pytest.raises(OMSInvariantError, match="checksum"):
        replace(bad, payload_checksum="0" * 64).verify()


def test_cancel_and_replace_fill_races_have_explicit_legal_paths(tmp_path: Path) -> None:
    for action in ("cancel", "replace"):
        repository = repo(tmp_path / action)
        service = PaperOMSService(repository)
        queued = service.queue(command(action))
        service.process_outbox(broker(volume=0))
        order = repository.get(queued.order_id)
        pending = (
            service.request_cancel(order.order_id, NOW)
            if action == "cancel"
            else service.request_replace(order.order_id, NOW)
        )
        event = service._event(
            pending,
            OMSState.PARTIALLY_FILLED,
            pending.state,
            "late_fill",
            f"late-fill-{action}",
            {"quantity": 1},
            event_time=NOW,
        )
        result = repository.transition_fill(
            order.order_id,
            event,
            expected_version=pending.version,
            fill_id=f"fill-{action}",
            business_identity=f"business-{action}",
            quantity=1,
            price=Decimal("10"),
            event_time=NOW,
        )
        assert result.state is OMSState.PARTIALLY_FILLED


def test_reconciliation_mismatch_kills_new_risk(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    PaperOMSService(repository).queue(command())
    report = reconcile_oms(repository, broker())
    assert report.status == "passed"
    paper = broker()
    paper.submit(PaperOrderRequest("unknown", "unknown", "2330", "buy", 1))
    failed = reconcile_oms(repository, paper)
    assert failed.kill_new_risk
    assert failed.mismatches[0].category == "unknown_broker_order"


def test_database_lock_corruption_and_migration(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == repository.schema_version
    lock = sqlite3.connect(repository.path)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(OMSPersistenceError, match="locked"):
            PaperOMSService(repository).queue(command())
    finally:
        lock.rollback()
        lock.close()
    repository.path.write_bytes(b"not-a-database")
    with pytest.raises(OMSPersistenceError, match="integrity"):
        repository.integrity_check()


def test_duplicate_fill_business_identity_conflict_is_rejected(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    order = PaperOMSService(repository).queue(command())
    assert repository.record_fill(order.order_id, "f1", "same", 1, Decimal("10"), NOW)
    assert not repository.record_fill(order.order_id, "f2", "same", 1, Decimal("10"), NOW)
    with pytest.raises(OMSPersistenceError):
        repository.record_fill(order.order_id, "f3", "same", 2, Decimal("10"), NOW)


def test_cli_requires_explicit_paper_and_has_no_live_flag() -> None:
    from island_quant.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["paper-init"])
    parsed = parser.parse_args(["paper-init", "--paper"])
    assert parsed.paper is True
    assert "--live" not in parser.format_help()


def test_persisted_event_document_detects_tampering(tmp_path: Path) -> None:
    repository = repo(tmp_path)
    order = PaperOMSService(repository).queue(command())
    with sqlite3.connect(repository.path) as connection:
        raw = connection.execute(
            "SELECT document FROM events WHERE order_id=? ORDER BY sequence LIMIT 1",
            (order.order_id,),
        ).fetchone()[0]
        document = json.loads(raw)
        original_event_id = document["event_id"]
        document["event_id"] = "0" * 64
        connection.execute(
            "UPDATE events SET document=? WHERE event_id=?",
            (json.dumps(document), original_event_id),
        )
    with pytest.raises(OMSInvariantError, match="identity"):
        repository.events(order.order_id)
