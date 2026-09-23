"""One-cycle, supervisor-friendly paper operations runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

from island_quant.brokers.paper import DeterministicPaperBroker, PaperMarketEvent
from island_quant.oms.repository import SQLiteOMSRepository
from island_quant.oms.service import PaperOMSService
from island_quant.operations.accounting import PaperAccountingProjector
from island_quant.operations.lifecycle import PaperServiceLifecycle
from island_quant.operations.monitoring import (
    AlertLevel,
    OperationsStateStore,
    PaperRiskLimits,
    enforce_paper_risk,
)
from island_quant.operations.reports import PaperDailyReport, write_daily_report
from island_quant.operations.scheduler import STANDARD_JOBS, PaperScheduler, RunState
from island_quant.operations.strategy import (
    MARKET_TIMEZONE,
    ExactPaperTargetReader,
    PaperTargetExecutor,
    PaperTargetSnapshot,
    paper_target_snapshot_document,
)


@dataclass(frozen=True, slots=True)
class PaperRuntimeResult:
    environment: str
    session: str
    safe_mode: bool
    job_states: tuple[tuple[str, str], ...]
    projection_version: str | None
    report_version: str | None


class PaperRuntime:
    def __init__(
        self,
        oms: SQLiteOMSRepository,
        service: PaperOMSService,
        broker: DeterministicPaperBroker,
        state: OperationsStateStore,
        scheduler: PaperScheduler,
        projector: PaperAccountingProjector,
        report_root: Path,
        market: tuple[PaperMarketEvent, ...],
        risk_limits: PaperRiskLimits | None = None,
        target_reader: ExactPaperTargetReader | None = None,
        target_executor: PaperTargetExecutor | None = None,
        target_artifact_version: str | None = None,
    ) -> None:
        self.oms = oms
        self.service = service
        self.broker = broker
        self.state = state
        self.scheduler = scheduler
        self.projector = projector
        self.report_root = report_root
        self.market = market
        self.risk_limits = risk_limits or PaperRiskLimits()
        self.target_reader = target_reader
        self.target_executor = target_executor
        self.target_artifact_version = target_artifact_version
        if (target_reader is None) != (target_executor is None) or (
            target_reader is None
        ) != (target_artifact_version is None):
            raise ValueError(
                "paper strategy reader, executor and artifact version are all required"
            )
        self.target_snapshot: PaperTargetSnapshot | None = None
        self.projection_version: str | None = None
        self.report_version: str | None = None

    def run_once(self, session: str, as_of: datetime) -> PaperRuntimeResult:
        self.target_snapshot = None
        lifecycle = PaperServiceLifecycle(self.oms, self.broker, self.state)
        health = lifecycle.startup(as_of)
        if not health.ready:
            return PaperRuntimeResult("paper", session, True, (), None, None)
        if not self.scheduler.acquire_leader(timedelta(minutes=2)):
            self.state.enter_safe_mode("scheduler_leader_unavailable")
            return PaperRuntimeResult("paper", session, True, (), None, None)
        handlers = {
            "data_freshness_check": lambda: self._data_freshness(session, as_of),
            "decision_snapshot": lambda: self._decision_snapshot(session, as_of),
            "feature_inference": lambda: self._feature_inference(as_of),
            "target_generation": lambda: self._target_generation(as_of),
            "risk_evaluation": lambda: self._risk(as_of),
            "paper_order_submission": self._submit,
            "reconciliation": lambda: self._reconcile(as_of),
            "mark_to_market": lambda: self._project(as_of),
            "daily_report": lambda: self._report(session, as_of),
            "health_heartbeat": lambda: self.state.heartbeat(as_of),
        }
        states: list[tuple[str, str]] = []
        sessions = tuple(item.isoformat() for item in self.projector.trading_sessions)
        for job in STANDARD_JOBS:
            state = self.scheduler.run(
                job,
                session=session,
                scheduled_at=as_of,
                trading_sessions=sessions,
                handler=handlers[job.job_id],
            )
            states.append((job.job_id, state.value))
            if state in {RunState.RETRY, RunState.DEAD_LETTER}:
                self.state.enter_safe_mode(f"job_failed:{job.job_id}")
                break
        final = lifecycle.health(as_of)
        return PaperRuntimeResult(
            "paper",
            session,
            final.safe_mode,
            tuple(states),
            self.projection_version,
            self.report_version,
        )

    def _data_freshness(self, session: str, as_of: datetime) -> None:
        session_date = datetime.fromisoformat(session).date()
        eligible = [
            item.event_time
            for item in self.market
            if item.event_time <= as_of
            and item.event_time.astimezone(MARKET_TIMEZONE).date() == session_date
        ]
        if not eligible:
            raise RuntimeError("no pinned market event is available for the execution session")
        age = Decimal(str((as_of - max(eligible)).total_seconds()))
        self.state.metric("last_market_data_timestamp", max(eligible).isoformat(), as_of)
        self.state.metric("data_latency", age, as_of)
        failures = enforce_paper_risk(
            self.state, {"market_data_age_seconds": age}, self.risk_limits, as_of
        )
        if failures:
            raise RuntimeError("market data freshness risk failed")

    def _risk(self, as_of: datetime) -> None:
        failures = enforce_paper_risk(
            self.state, self._risk_metrics(as_of), self.risk_limits, as_of
        )
        if failures:
            raise RuntimeError("paper risk limit failed before target execution")
        if self.target_snapshot is not None:
            if self.target_executor is None:
                raise RuntimeError("paper target executor is unavailable")
            order_ids = self.target_executor.execute(
                self.target_snapshot, self.state.current_portfolio(), created_at=as_of
            )
            self.state.metric("target_order_count", len(order_ids), as_of)
        failures = enforce_paper_risk(
            self.state, self._risk_metrics(as_of), self.risk_limits, as_of
        )
        if failures:
            raise RuntimeError("paper risk limit failed")

    def _risk_metrics(self, as_of: datetime) -> dict[str, Decimal]:
        portfolio = self.state.current_portfolio()
        session_orders = [
            order for order in self.oms.list_orders() if order.created_at.date() == as_of.date()
        ]
        return {
            "gross_exposure": Decimal(str(portfolio["gross_exposure"]))
            if portfolio
            else Decimal("0"),
            "drawdown": Decimal(str(portfolio["drawdown"])) if portfolio else Decimal("0"),
            "orders": Decimal(len(session_orders)),
            "notional": self.oms.reserved_cash(),
        }

    def _decision_snapshot(self, session: str, as_of: datetime) -> None:
        if self.target_reader is None or self.target_artifact_version is None:
            self.state.clear_current_target()
            self.state.metric("last_successful_decision", "no_strategy_configured", as_of)
            return
        self.target_snapshot = self.target_reader.read(
            self.target_artifact_version,
            session=datetime.fromisoformat(session).date(),
            as_of=as_of,
        )
        self.state.publish_target_snapshot(
            self.target_snapshot.artifact_version,
            paper_target_snapshot_document(self.target_snapshot),
            as_of,
        )
        self.state.metric("last_successful_decision", self.target_snapshot.artifact_version, as_of)

    def _feature_inference(self, as_of: datetime) -> None:
        if self.target_snapshot is None:
            self.state.metric("feature_inference", "no_strategy_configured", as_of)
            return
        self.state.metric(
            "feature_inference",
            self.target_snapshot.lineage_value("model_artifact_version"),
            as_of,
        )

    def _target_generation(self, as_of: datetime) -> None:
        value = (
            self.target_snapshot.artifact_version
            if self.target_snapshot is not None
            else "no_new_targets"
        )
        self.state.metric("target_generation", value, as_of)

    def _submit(self) -> None:
        self.service.process_outbox(self.broker)

    def _project(self, as_of: datetime) -> None:
        result = self.projector.project(as_of)
        self.projection_version = result.projection_version

    def _reconcile(self, as_of: datetime) -> None:
        result = self.projector.project(as_of)
        self.projection_version = result.projection_version
        oms_fill_ids = {str(item["fill_id"]) for item in self.oms.fills()}
        projected = set(result.applied_fill_ids)
        mismatches = 0 if oms_fill_ids == projected else 1
        self.state.metric("reconciliation_mismatches", mismatches, as_of)
        if mismatches or result.snapshot.reconciliation_residual != Decimal("0"):
            self.state.enter_safe_mode("paper_accounting_reconciliation_failed")
            self.state.raise_alert(
                AlertLevel.CRITICAL,
                "position_mismatch",
                "OMS fills and paper accounting projection differ",
                as_of,
                cooldown=timedelta(minutes=30),
            )
            raise RuntimeError("paper accounting reconciliation failed")

    def _report(self, session: str, as_of: datetime) -> None:
        portfolio = self.state.current_portfolio()
        if portfolio is None or not bool(portfolio["valuation_complete"]):
            raise RuntimeError("complete paper portfolio valuation is required")
        position_rows = cast(list[dict[str, object]], portfolio["positions"])
        positions = tuple(
            (str(item["instrument_id"]), int(str(item["quantity"]))) for item in position_rows
        )
        lineage = self.target_snapshot.lineage if self.target_snapshot is not None else ()
        report = PaperDailyReport(
            schema_version=2,
            environment="paper",
            session=session,
            generated_at=as_of,
            data_freshness="fresh",
            strategy_version=(
                self.target_snapshot.lineage_value("strategy_version")
                if self.target_snapshot is not None
                else "no-strategy-configured"
            ),
            model_version=(
                self.target_snapshot.lineage_value("model_artifact_version")
                if self.target_snapshot is not None
                else "no-model-configured"
            ),
            artifact_version=(
                self.target_snapshot.artifact_version
                if self.target_snapshot is not None
                else str(portfolio["projection_version"])
            ),
            order_count=len(self.oms.list_orders()),
            fill_count=len(self.oms.fills()),
            rejection_count=sum(
                order.state.value.endswith("Rejected") for order in self.oms.list_orders()
            ),
            positions=positions,
            cash=Decimal(str(portfolio["cash"])),
            nav=Decimal(str(portfolio["nav"])),
            daily_pnl=Decimal(str(portfolio["daily_pnl"])),
            costs=Decimal(str(portfolio["fees"])) + Decimal(str(portfolio["taxes"])),
            gross_exposure=Decimal(str(portfolio["gross_exposure"])),
            risk_events=(),
            reconciliation_status="passed",
            alert_count=0,
            safe_mode=False,
            data_quality_issues=(),
            target_artifact_version=(
                self.target_snapshot.artifact_version
                if self.target_snapshot is not None
                else None
            ),
            portfolio_projection_version=str(portfolio["projection_version"]),
            lineage=lineage,
        )
        self.report_version = write_daily_report(self.report_root, report)


def runtime_result_dict(result: PaperRuntimeResult) -> dict[str, object]:
    return asdict(result)
