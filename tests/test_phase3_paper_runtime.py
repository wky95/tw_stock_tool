from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from island_quant.brokers.paper import (
    DeterministicPaperBroker,
    PaperBrokerPolicy,
    PaperMarketEvent,
)
from island_quant.dashboard.app import create_app
from island_quant.dashboard.operations import PaperOperationsQuery
from island_quant.dashboard.service import DashboardQueryService
from island_quant.domain.models import Market
from island_quant.oms.models import OMSState
from island_quant.oms.repository import SQLiteOMSRepository
from island_quant.oms.service import PaperOMSService, PaperOrderCommand
from island_quant.operations.accounting import (
    PaperAccountingPolicy,
    PaperAccountingProjector,
    PaperInstrumentReference,
    PaperMark,
)
from island_quant.operations.monitoring import OperationsStateStore
from island_quant.operations.runtime import PaperRuntime
from island_quant.operations.scheduler import FixedClock, PaperScheduler

NOW = datetime(2025, 1, 2, 9, tzinfo=UTC)
SESSIONS = (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6))


def setup(tmp_path: Path) -> tuple[SQLiteOMSRepository, OperationsStateStore]:
    oms = SQLiteOMSRepository(tmp_path / "oms.sqlite")
    oms.initialize()
    state = OperationsStateStore(tmp_path / "operations.sqlite")
    state.initialize()
    return oms, state


def order(key: str, quantity: int, cash: str = "1000000") -> PaperOrderCommand:
    return PaperOrderCommand(
        f"client-{key}", key, "2330", "buy", quantity, Decimal("10"), Decimal(cash), NOW
    )


def paper(volume: int = 100000) -> DeterministicPaperBroker:
    return DeterministicPaperBroker(
        (PaperMarketEvent("2330", NOW, Decimal("10"), volume),), PaperBrokerPolicy()
    )


def projector(oms: SQLiteOMSRepository, state: OperationsStateStore) -> PaperAccountingProjector:
    return PaperAccountingProjector(
        oms,
        state,
        PaperAccountingPolicy(),
        (PaperInstrumentReference("2330", Market.TWSE, "instrument-fixture-v1"),),
        SESSIONS,
        (
            PaperMark(
                "2330",
                date(2025, 1, 3),
                Decimal("10.1"),
                datetime(2025, 1, 3, 13, 30, tzinfo=UTC),
                "fixture_close",
            ),
            PaperMark(
                "2330",
                date(2025, 1, 6),
                Decimal("10.2"),
                datetime(2025, 1, 6, 13, 30, tzinfo=UTC),
                "fixture_close",
            ),
        ),
    )


def test_pending_buy_reservations_prevent_double_spending_and_release(tmp_path: Path) -> None:
    oms, _ = setup(tmp_path)
    service = PaperOMSService(oms)
    first = service.queue(order("one", 60, cash="1000"))
    assert first.state is OMSState.SUBMIT_PENDING
    assert oms.reserved_cash() == Decimal("620")
    second = service.queue(order("two", 60, cash="1000"))
    assert second.state is OMSState.RISK_REJECTED
    assert oms.reserved_cash() == Decimal("620")
    service.process_outbox(paper(volume=500))
    assert Decimal("100") < oms.reserved_cash() < Decimal("104")
    current = oms.get(first.order_id)
    service.request_cancel(current.order_id, NOW)
    service.apply_cancel_result(current.order_id, "cancelled", NOW)
    assert oms.reserved_cash() == Decimal("0")


def test_sell_reservation_uses_projected_available_position(tmp_path: Path) -> None:
    oms, _ = setup(tmp_path)
    service = PaperOMSService(oms)
    first = PaperOrderCommand(
        "sell-1", "sell-1", "2330", "sell", 70, Decimal("10"), Decimal("0"), NOW, 100
    )
    second = PaperOrderCommand(
        "sell-2", "sell-2", "2330", "sell", 40, Decimal("10"), Decimal("0"), NOW, 100
    )
    assert service.queue(first).state is OMSState.SUBMIT_PENDING
    assert service.queue(second).state is OMSState.RISK_REJECTED
    assert oms.reserved_sell_quantity("2330") == 70


def test_broker_rejection_releases_reservation_atomically(tmp_path: Path) -> None:
    oms, _ = setup(tmp_path)
    service = PaperOMSService(oms)
    queued = service.queue(order("reject", 10))
    rejecting = DeterministicPaperBroker(
        (PaperMarketEvent("2330", NOW, Decimal("10"), 1000),),
        PaperBrokerPolicy(reject_all=True),
    )
    service.process_outbox(rejecting)
    assert oms.get(queued.order_id).state is OMSState.REJECTED
    assert oms.reserved_cash() == Decimal("0")


def test_accounting_projection_is_exact_persistent_idempotent_and_settles(tmp_path: Path) -> None:
    oms, state = setup(tmp_path)
    service = PaperOMSService(oms)
    queued = service.queue(order("filled", 100))
    service.process_outbox(paper())
    assert oms.get(queued.order_id).state is OMSState.FILLED
    projection = projector(oms, state)
    trade = projection.project(NOW)
    assert trade.snapshot.position_quantities == (("TWSE:2330", 100),)
    assert trade.snapshot.settlement_payables == Decimal("1020.500")
    assert trade.snapshot.available_cash == Decimal("998979.500")
    assert trade.snapshot.reconciliation_residual == Decimal("0")
    assert projection.project(NOW).projection_version == trade.projection_version
    settled_at = datetime(2025, 1, 6, 16, tzinfo=UTC)
    settled = projection.project(settled_at)
    assert settled.snapshot.settlement_payables == Decimal("0")
    assert settled.snapshot.settled_cash == Decimal("998979.500")
    current = state.current_portfolio()
    assert current is not None
    assert current["projection_version"] == settled.projection_version
    assert current["valuation_complete"] is True


def test_projection_rejects_missing_reference_and_calendar(tmp_path: Path) -> None:
    oms, state = setup(tmp_path)
    service = PaperOMSService(oms)
    service.queue(order("filled", 1))
    service.process_outbox(paper())
    missing = PaperAccountingProjector(oms, state, PaperAccountingPolicy(), (), SESSIONS)
    try:
        missing.project(NOW)
    except RuntimeError as exc:
        assert "instrument reference" in str(exc)
    else:
        raise AssertionError("missing reference must fail closed")
    bad_calendar = PaperAccountingProjector(
        oms,
        state,
        PaperAccountingPolicy(),
        (PaperInstrumentReference("2330", Market.TWSE, "v1"),),
        (date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 7)),
    )
    try:
        bad_calendar.project(NOW)
    except RuntimeError as exc:
        assert "calendar" in str(exc)
    else:
        raise AssertionError("fill outside calendar must fail closed")


def test_dashboard_exposes_persisted_portfolio_without_write_endpoints(tmp_path: Path) -> None:
    oms, state = setup(tmp_path)
    service = PaperOMSService(oms)
    service.queue(order("filled", 100))
    service.process_outbox(paper())
    projector(oms, state).project(NOW)
    scheduler = PaperScheduler(tmp_path / "scheduler.sqlite", FixedClock(NOW))
    scheduler.initialize()
    app = create_app(
        DashboardQueryService(operations_query=PaperOperationsQuery(oms, state, scheduler))
    )
    client = TestClient(app)
    payload = client.get("/api/dashboard/paper-operations").json()
    assert payload["cash_nav"]["status"] == "available"
    assert payload["positions"] == [{"instrument_id": "TWSE:2330", "quantity": 100}]
    assert client.post("/api/dashboard/paper-operations").status_code == 405


def test_runtime_executes_versioned_jobs_and_writes_daily_report(tmp_path: Path) -> None:
    oms, state = setup(tmp_path)
    PaperOMSService(oms).queue(order("runtime", 100))
    scheduler = PaperScheduler(tmp_path / "scheduler.sqlite", FixedClock(NOW))
    scheduler.initialize()
    market = (PaperMarketEvent("2330", NOW, Decimal("10"), 100000),)
    broker = DeterministicPaperBroker(market, PaperBrokerPolicy())
    runtime = PaperRuntime(
        oms,
        PaperOMSService(oms),
        broker,
        state,
        scheduler,
        projector(oms, state),
        tmp_path / "artifacts",
        market,
    )
    result = runtime.run_once("2025-01-02", NOW)
    assert not result.safe_mode
    assert len(result.job_states) == 10
    assert {value for _, value in result.job_states} == {"completed"}
    assert result.projection_version is not None
    assert result.report_version is not None
    assert len(oms.fills()) == 1
    portfolio = state.current_portfolio()
    assert portfolio is not None
    assert portfolio["positions"] == [{"instrument_id": "TWSE:2330", "quantity": 100}]
    assert (
        tmp_path / "artifacts" / "paper-reports" / result.report_version / "report.json"
    ).exists()
    assert len(scheduler.runs()) == 10


def test_runtime_stale_market_data_enters_safe_mode_and_stops_jobs(tmp_path: Path) -> None:
    oms, state = setup(tmp_path)
    later = datetime(2025, 1, 2, 12, tzinfo=UTC)
    scheduler = PaperScheduler(tmp_path / "scheduler.sqlite", FixedClock(later))
    scheduler.initialize()
    market = (PaperMarketEvent("2330", NOW, Decimal("10"), 100000),)
    broker = DeterministicPaperBroker(market, PaperBrokerPolicy())
    runtime = PaperRuntime(
        oms,
        PaperOMSService(oms),
        broker,
        state,
        scheduler,
        projector(oms, state),
        tmp_path / "artifacts",
        market,
    )
    result = runtime.run_once("2025-01-02", later)
    assert result.safe_mode
    assert result.job_states == (("data_freshness_check", "retry"),)
