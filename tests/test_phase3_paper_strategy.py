from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from island_quant.brokers.paper import (
    DeterministicPaperBroker,
    PaperBrokerPolicy,
    PaperMarketEvent,
)
from island_quant.domain.models import Market
from island_quant.oms.models import OMSState
from island_quant.oms.repository import OMSPersistenceError, SQLiteOMSRepository
from island_quant.oms.service import PaperOMSService
from island_quant.operations.accounting import (
    PaperAccountingPolicy,
    PaperAccountingProjector,
    PaperInstrumentReference,
    PaperMark,
)
from island_quant.operations.monitoring import OperationsStateStore
from island_quant.operations.runtime import PaperRuntime
from island_quant.operations.scheduler import FixedClock, PaperScheduler
from island_quant.operations.strategy import (
    REQUIRED_LINEAGE,
    ExactPaperTargetReader,
    PaperStrategyRiskPolicy,
    PaperTargetExecutor,
)
from island_quant.pipeline.artifacts import ExactArtifactStore

NOW = datetime(2025, 1, 2, 9, tzinfo=UTC)
DECISION = datetime(2025, 1, 1, 8, tzinfo=UTC)
SESSIONS = (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def lineage() -> dict[str, str]:
    return {name: digest(name) for name in REQUIRED_LINEAGE}


def record(
    *,
    weight: str = "0.10",
    instrument_id: str = "2330",
    market: str = "TWSE",
    execution_session: str = "2025-01-02",
    eligible: object = True,
    decision_time: str = DECISION.isoformat(),
    available_at: str = datetime(2025, 1, 1, 7, tzinfo=UTC).isoformat(),
) -> dict[str, object]:
    return {
        "instrument_id": instrument_id,
        "market": market,
        "execution_session": execution_session,
        "decision_time": decision_time,
        "available_at": available_at,
        "target_weight": weight,
        "reference_price": "10",
        "eligible": eligible,
    }


def publish(
    root: Path,
    records: list[dict[str, object]] | None = None,
    *,
    completeness: str = "validated",
    classification: str = "paper_candidate",
    source_lineage: dict[str, str] | None = None,
) -> str:
    manifest = ExactArtifactStore(root, "paper_target_snapshots", 1).publish(
        records or [record()],
        lineage=source_lineage or lineage(),
        completeness=completeness,
        classification=classification,
        created_at=NOW,
    )
    return manifest.artifact_version


def repository(tmp_path: Path) -> SQLiteOMSRepository:
    result = SQLiteOMSRepository(tmp_path / "oms.sqlite")
    result.initialize()
    return result


def executor(oms: SQLiteOMSRepository) -> PaperTargetExecutor:
    return PaperTargetExecutor(
        oms,
        PaperOMSService(oms),
        {"2330": Market.TWSE},
        {"2330": Decimal("10")},
    )


def test_reader_requires_exact_validated_complete_pit_snapshot(tmp_path: Path) -> None:
    reader = ExactPaperTargetReader(tmp_path)
    version = publish(tmp_path)
    snapshot = reader.read(version, session=SESSIONS[0], as_of=NOW)
    assert snapshot.artifact_version == version
    assert snapshot.targets[0].target_weight == Decimal("0.10")
    for invalid in ("latest", "../escape", "A" * 64):
        with pytest.raises(ValueError, match="canonical"):
            reader.read(invalid, session=SESSIONS[0], as_of=NOW)
    for kwargs in (
        {"completeness": "exploratory"},
        {"classification": "research_only"},
        {"source_lineage": {"strategy_version": digest("only")}},
    ):
        rejected = publish(tmp_path, **kwargs)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError):
            reader.read(rejected, session=SESSIONS[0], as_of=NOW)


@pytest.mark.parametrize(
    "bad_record,error",
    [
        (record(weight="-0.1"), "long-only"),
        (record(weight="1.1"), "long-only"),
        (record(eligible=False), "ineligible"),
        (record(eligible="false"), "invalid"),
        (record(execution_session="2025-01-03"), "session mismatch"),
        (
            record(decision_time=datetime(2025, 1, 2, 8, tzinfo=UTC).isoformat()),
            "after its decision session",
        ),
        (
            record(available_at=datetime(2025, 1, 1, 9, tzinfo=UTC).isoformat()),
            "point-in-time",
        ),
        (
            record(decision_time=datetime(2025, 1, 3, tzinfo=UTC).isoformat()),
            "after its decision session",
        ),
    ],
)
def test_reader_rejects_unsafe_target_records(
    tmp_path: Path, bad_record: dict[str, object], error: str
) -> None:
    version = publish(tmp_path, [bad_record])
    with pytest.raises(RuntimeError, match=error):
        ExactPaperTargetReader(tmp_path).read(version, session=SESSIONS[0], as_of=NOW)


def test_reader_detects_duplicates_and_cas_tampering(tmp_path: Path) -> None:
    version = publish(tmp_path, [record(), record()])
    reader = ExactPaperTargetReader(tmp_path)
    with pytest.raises(RuntimeError, match="duplicate"):
        reader.read(version, session=SESSIONS[0], as_of=NOW)
    clean = publish(tmp_path, [record(weight="0.11")])
    data = tmp_path / "paper_target_snapshots" / clean / "records.jsonl"
    data.write_text(data.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="checksum"):
        reader.read(clean, session=SESSIONS[0], as_of=NOW)


def test_target_execution_is_integer_idempotent_reserved_and_lineaged(tmp_path: Path) -> None:
    version = publish(tmp_path)
    snapshot = ExactPaperTargetReader(tmp_path).read(version, session=SESSIONS[0], as_of=NOW)
    oms = repository(tmp_path)
    target_executor = executor(oms)
    order_ids = target_executor.execute(snapshot, None, created_at=NOW)
    assert len(order_ids) == 1
    order = oms.get(order_ids[0])
    assert order.quantity == 10000
    assert order.state is OMSState.SUBMIT_PENDING
    assert oms.reserved_cash() == Decimal("100193.0000")
    persisted = oms.order_lineage(order.order_id)
    assert persisted is not None
    assert persisted["target_artifact_version"] == version
    assert persisted["strategy_version"] == lineage()["strategy_version"]
    assert persisted["target_weight"] == "0.10"
    assert dict(oms.events(order.order_id)[0].payload)["target_artifact_version"] == version
    assert target_executor.execute(snapshot, None, created_at=NOW) == ()
    assert len(oms.list_orders()) == 1


def test_order_lineage_tampering_disagrees_with_checksummed_journal(tmp_path: Path) -> None:
    snapshot = ExactPaperTargetReader(tmp_path).read(
        publish(tmp_path), session=SESSIONS[0], as_of=NOW
    )
    oms = repository(tmp_path)
    order_id = executor(oms).execute(snapshot, None, created_at=NOW)[0]
    with sqlite3.connect(oms.path) as connection:
        connection.execute(
            "UPDATE order_lineage SET strategy_version=? WHERE order_id=?",
            (digest("tampered"), order_id),
        )
    with pytest.raises(OMSPersistenceError, match="event journal"):
        oms.order_lineage(order_id)


def test_schema_v2_database_migrates_forward_without_losing_orders(tmp_path: Path) -> None:
    oms = repository(tmp_path)
    with sqlite3.connect(oms.path) as connection:
        connection.execute("DROP TABLE order_lineage")
        connection.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
        connection.execute("PRAGMA user_version=2")
    oms.initialize()
    with sqlite3.connect(oms.path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='order_lineage'"
        ).fetchone()
    assert version == 3
    assert table == ("order_lineage",)


def test_target_execution_fails_closed_on_reference_and_risk_mismatch(tmp_path: Path) -> None:
    version = publish(tmp_path, [record(weight="0.21")])
    snapshot = ExactPaperTargetReader(tmp_path).read(version, session=SESSIONS[0], as_of=NOW)
    oms = repository(tmp_path)
    with pytest.raises(RuntimeError, match="single-position"):
        executor(oms).execute(snapshot, None, created_at=NOW)
    safe = ExactPaperTargetReader(tmp_path).read(
        publish(tmp_path, [record(weight="0.10")]), session=SESSIONS[0], as_of=NOW
    )
    wrong_price = PaperTargetExecutor(
        oms, PaperOMSService(oms), {"2330": Market.TWSE}, {"2330": Decimal("9")}
    )
    with pytest.raises(RuntimeError, match="execution price"):
        wrong_price.execute(safe, None, created_at=NOW)
    incomplete = {
        "valuation_complete": False,
        "positions": [],
        "nav": "1000000",
        "available_cash": "1000000",
    }
    with pytest.raises(RuntimeError, match="complete portfolio"):
        executor(oms).execute(safe, incomplete, created_at=NOW)


def test_projected_pending_orders_prevent_duplicate_exposure(tmp_path: Path) -> None:
    version = publish(tmp_path)
    snapshot = ExactPaperTargetReader(tmp_path).read(version, session=SESSIONS[0], as_of=NOW)
    oms = repository(tmp_path)
    target_executor = executor(oms)
    assert len(target_executor.execute(snapshot, None, created_at=NOW)) == 1
    assert target_executor.execute(snapshot, None, created_at=NOW) == ()
    assert oms.reserved_cash() == Decimal("100193.0000")


def test_zero_target_sells_only_owned_integer_position(tmp_path: Path) -> None:
    version = publish(tmp_path, [record(weight="0")])
    snapshot = ExactPaperTargetReader(tmp_path).read(version, session=SESSIONS[0], as_of=NOW)
    oms = repository(tmp_path)
    portfolio: dict[str, object] = {
        "valuation_complete": True,
        "positions": [{"instrument_id": "TWSE:2330", "quantity": 75}],
        "nav": "1000",
        "available_cash": "250",
    }
    order_id = executor(oms).execute(snapshot, portfolio, created_at=NOW)[0]
    order = oms.get(order_id)
    assert (order.side, order.quantity) == ("sell", 75)
    assert oms.reserved_sell_quantity("2330") == 75


def test_target_must_cover_every_existing_position(tmp_path: Path) -> None:
    snapshot = ExactPaperTargetReader(tmp_path).read(
        publish(tmp_path), session=SESSIONS[0], as_of=NOW
    )
    oms = repository(tmp_path)
    portfolio: dict[str, object] = {
        "valuation_complete": True,
        "positions": [{"instrument_id": "TPEx:6488", "quantity": 1}],
        "nav": "1000000",
        "available_cash": "1000000",
    }
    with pytest.raises(RuntimeError, match="omits an existing position"):
        executor(oms).execute(snapshot, portfolio, created_at=NOW)
    assert oms.list_orders() == ()


def test_runtime_executes_exact_target_through_report_lineage(tmp_path: Path) -> None:
    version = publish(tmp_path / "artifacts")
    oms = repository(tmp_path)
    state = OperationsStateStore(tmp_path / "operations.sqlite")
    state.initialize()
    scheduler = PaperScheduler(tmp_path / "scheduler.sqlite", FixedClock(NOW))
    scheduler.initialize()
    market = (PaperMarketEvent("2330", NOW, Decimal("10"), 100000),)
    instruments = (PaperInstrumentReference("2330", Market.TWSE, digest("instrument")),)
    marks = (PaperMark("2330", SESSIONS[0], Decimal("10"), NOW, "fixture"),)
    service = PaperOMSService(oms)
    runtime = PaperRuntime(
        oms,
        service,
        DeterministicPaperBroker(market, PaperBrokerPolicy()),
        state,
        scheduler,
        PaperAccountingProjector(
            oms, state, PaperAccountingPolicy(), instruments, SESSIONS, marks
        ),
        tmp_path / "artifacts",
        market,
        target_reader=ExactPaperTargetReader(tmp_path / "artifacts"),
        target_executor=PaperTargetExecutor(
            oms, service, {"2330": Market.TWSE}, {"2330": Decimal("10")}
        ),
        target_artifact_version=version,
    )
    result = runtime.run_once("2025-01-02", NOW)
    assert result.safe_mode is False
    assert len(oms.list_orders()) == 1
    assert len(oms.fills()) == 1
    assert result.report_version is not None
    report_path = (
        tmp_path / "artifacts" / "paper-reports" / result.report_version / "report.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["target_artifact_version"] == version
    assert report["portfolio_projection_version"] == result.projection_version
    assert dict(report["lineage"])["model_artifact_version"] == lineage()[
        "model_artifact_version"
    ]


def test_runtime_constructor_requires_complete_strategy_wiring(tmp_path: Path) -> None:
    oms = repository(tmp_path)
    state = OperationsStateStore(tmp_path / "state.sqlite")
    state.initialize()
    scheduler = PaperScheduler(tmp_path / "scheduler.sqlite", FixedClock(NOW))
    scheduler.initialize()
    market = (PaperMarketEvent("2330", NOW, Decimal("10"), 100),)
    with pytest.raises(ValueError, match="all required"):
        PaperRuntime(
            oms,
            PaperOMSService(oms),
            DeterministicPaperBroker(market, PaperBrokerPolicy()),
            state,
            scheduler,
            PaperAccountingProjector(oms, state, PaperAccountingPolicy(), (), SESSIONS),
            tmp_path,
            market,
            target_reader=ExactPaperTargetReader(tmp_path),
        )


def test_risk_policy_is_versioned_and_rejects_unsafe_values() -> None:
    assert PaperStrategyRiskPolicy().version == "paper-target-risk-v1"
    with pytest.raises(ValueError):
        PaperStrategyRiskPolicy(maximum_gross_exposure=Decimal("1.1"))
