"""Offline multi-session PAPER soak runner and immutable drill report."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from island_quant.brokers.paper import (
    DeterministicPaperBroker,
    PaperBrokerPolicy,
    PaperMarketEvent,
)
from island_quant.config import AppSettings
from island_quant.domain.models import Market
from island_quant.oms.models import TERMINAL_STATES
from island_quant.oms.repository import SQLiteOMSRepository
from island_quant.oms.service import PaperOMSService
from island_quant.operations.accounting import (
    PaperAccountingPolicy,
    PaperAccountingProjector,
    PaperInstrumentReference,
    PaperMark,
)
from island_quant.operations.monitoring import OperationsStateStore
from island_quant.operations.runtime import PaperRuntime, PaperRuntimeResult
from island_quant.operations.scheduler import FixedClock, PaperScheduler
from island_quant.operations.strategy import (
    MARKET_TIMEZONE,
    ExactPaperTargetReader,
    PaperStrategyRiskPolicy,
    PaperTargetExecutor,
)
from island_quant.pipeline.artifacts import (
    ExactArtifactStore,
    canonical_json,
    require_exact_version,
)


@dataclass(frozen=True, slots=True)
class SoakInstrument:
    instrument_id: str
    market: Market
    reference_version: str


@dataclass(frozen=True, slots=True)
class SoakCycle:
    session: date
    as_of: datetime
    market: tuple[PaperMarketEvent, ...]
    target_artifact_version: str | None
    broker_participation_cap: Decimal


@dataclass(frozen=True, slots=True)
class PaperSoakPlan:
    schema_version: int
    plan_version: str
    sessions: tuple[date, ...]
    instruments: tuple[SoakInstrument, ...]
    cycles: tuple[SoakCycle, ...]
    require_final_settlement: bool = True
    verify_duplicate_sessions: bool = True

    @classmethod
    def read(cls, path: Path) -> PaperSoakPlan:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("paper soak plan must be a JSON object")
        expected = {
            "schema_version",
            "sessions",
            "instruments",
            "cycles",
            "require_final_settlement",
            "verify_duplicate_sessions",
        }
        if raw.keys() != expected or raw["schema_version"] != 1:
            raise ValueError("paper soak plan schema is invalid")
        if not all(
            isinstance(raw[name], list) for name in ("sessions", "instruments", "cycles")
        ) or not all(
            isinstance(raw[name], bool)
            for name in ("require_final_settlement", "verify_duplicate_sessions")
        ):
            raise ValueError("paper soak plan field types are invalid")
        sessions = tuple(date.fromisoformat(str(item)) for item in raw["sessions"])
        instruments = tuple(_instrument(item) for item in raw["instruments"])
        cycles = tuple(_cycle(item) for item in raw["cycles"])
        version = hashlib.sha256(canonical_json(raw)).hexdigest()
        result = cls(
            1,
            version,
            sessions,
            instruments,
            cycles,
            bool(raw["require_final_settlement"]),
            bool(raw["verify_duplicate_sessions"]),
        )
        result.validate()
        return result

    def validate(self) -> None:
        require_exact_version(self.plan_version)
        if not self.sessions or tuple(sorted(set(self.sessions))) != self.sessions:
            raise ValueError("paper soak sessions must be non-empty, unique, and sorted")
        if not self.instruments or not self.cycles:
            raise ValueError("paper soak requires instruments and cycles")
        instrument_ids = [item.instrument_id for item in self.instruments]
        if len(instrument_ids) != len(set(instrument_ids)):
            raise ValueError("paper soak instrument IDs must be unique")
        cycle_sessions = [item.session for item in self.cycles]
        if cycle_sessions != sorted(set(cycle_sessions)):
            raise ValueError("paper soak cycle sessions must be unique and sorted")
        known = set(instrument_ids)
        for cycle in self.cycles:
            if cycle.session not in self.sessions:
                raise ValueError("paper soak cycle is absent from pinned calendar")
            if cycle.as_of.tzinfo is None or cycle.as_of.utcoffset() is None:
                raise ValueError("paper soak timestamps must be timezone-aware")
            if cycle.as_of.astimezone(MARKET_TIMEZONE).date() != cycle.session:
                raise ValueError("paper soak as-of does not match its market session")
            if not cycle.broker_participation_cap.is_finite() or not (
                Decimal("0") < cycle.broker_participation_cap <= Decimal("1")
            ):
                raise ValueError("paper soak broker participation cap must be in (0, 1]")
            symbols = [item.instrument_id for item in cycle.market]
            if not cycle.market or len(symbols) != len(set(symbols)):
                raise ValueError("paper soak market events must be non-empty and unique")
            if set(symbols) != known:
                raise ValueError("paper soak market coverage is incomplete")
            for event in cycle.market:
                if event.event_time.tzinfo is None or event.event_time > cycle.as_of:
                    raise ValueError("paper soak market event time is invalid")
                if event.event_time.astimezone(MARKET_TIMEZONE).date() != cycle.session:
                    raise ValueError("paper soak market event session mismatch")
                if (
                    not event.reference_price.is_finite()
                    or event.reference_price <= 0
                    or event.volume < 0
                ):
                    raise ValueError("paper soak market price or volume is invalid")


@dataclass(frozen=True, slots=True)
class SoakCycleResult:
    session: str
    target_artifact_version: str | None
    safe_mode: bool
    jobs_completed: int
    order_count: int
    fill_count: int
    duplicate_replay_clean: bool
    projection_version: str | None


@dataclass(frozen=True, slots=True)
class PaperSoakResult:
    plan_version: str
    status: str
    dry_run: bool
    cycles: tuple[SoakCycleResult, ...]
    checks: tuple[tuple[str, str], ...]
    soak_report_version: str | None


class PaperSoakRunner:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.oms = SQLiteOMSRepository(settings.paper.oms_database_path)
        self.state = OperationsStateStore(settings.paper.operations_database_path)

    def run(self, plan: PaperSoakPlan, *, dry_run: bool) -> PaperSoakResult:
        plan.validate()
        target_reader = ExactPaperTargetReader(self.settings.research.artifact_root)
        for cycle in plan.cycles:
            if cycle.target_artifact_version is not None:
                target_reader.read(
                    cycle.target_artifact_version,
                    session=cycle.session,
                    as_of=cycle.as_of,
                )
        checks = self._plan_checks(plan)
        if dry_run:
            return PaperSoakResult(plan.plan_version, "validated", True, (), checks, None)
        self.oms.initialize()
        self.state.initialize()
        cycle_results: list[SoakCycleResult] = []
        marks = tuple(
            PaperMark(
                event.instrument_id,
                cycle.session,
                event.reference_price,
                event.event_time,
                "pinned_paper_soak_event",
            )
            for cycle in plan.cycles
            for event in cycle.market
        )
        failed_reason: str | None = None
        for cycle in plan.cycles:
            result, replay_clean, projection_version = self._cycle(plan, cycle, marks)
            cycle_results.append(
                SoakCycleResult(
                    cycle.session.isoformat(),
                    cycle.target_artifact_version,
                    result.safe_mode,
                    sum(state == "completed" for _, state in result.job_states),
                    len(self.oms.list_orders()),
                    len(self.oms.fills()),
                    replay_clean,
                    projection_version,
                )
            )
            if result.safe_mode:
                failed_reason = f"safe_mode:{cycle.session.isoformat()}"
                break
            if plan.verify_duplicate_sessions and not replay_clean:
                failed_reason = f"duplicate_replay_changed_state:{cycle.session.isoformat()}"
                break
        final_checks = list(checks)
        final_checks.extend(self._final_checks(plan, failed_reason))
        passed = all(value == "passed" for _, value in final_checks)
        status = "passed" if passed else "failed"
        report = {
            "schema_version": 1,
            "environment": "paper",
            "classification": "engineering_soak_not_live_performance",
            "plan_version": plan.plan_version,
            "status": status,
            "cycles": [asdict(item) for item in cycle_results],
            "checks": final_checks,
            "live_trading_enabled": False,
            "known_limitations": [
                "deterministic paper broker is not an order-book simulation",
                "engineering fixture costs and settlement require broker verification",
            ],
        }
        lineage = {"plan_version": plan.plan_version}
        lineage.update(
            {
                f"target_{index:04d}": version
                for index, version in enumerate(
                    sorted(
                        {
                            item.target_artifact_version
                            for item in plan.cycles
                            if item.target_artifact_version is not None
                        }
                    )
                )
            }
        )
        manifest = ExactArtifactStore(
            self.settings.research.artifact_root, "paper_soak_reports", 1
        ).publish(
            [report],
            lineage=lineage,
            completeness="validated" if passed else "incomplete",
            classification="paper_engineering_soak",
            created_at=plan.cycles[len(cycle_results) - 1].as_of,
        )
        return PaperSoakResult(
            plan.plan_version,
            status,
            False,
            tuple(cycle_results),
            tuple(final_checks),
            manifest.artifact_version,
        )

    def _cycle(
        self,
        plan: PaperSoakPlan,
        cycle: SoakCycle,
        marks: tuple[PaperMark, ...],
    ) -> tuple[PaperRuntimeResult, bool, str | None]:
        scheduler = PaperScheduler(
            self.settings.paper.scheduler_database_path,
            FixedClock(cycle.as_of),
            owner="paper-soak-runner",
        )
        scheduler.initialize()
        broker = DeterministicPaperBroker(
            cycle.market,
            PaperBrokerPolicy(participation_cap=cycle.broker_participation_cap),
        )
        service = PaperOMSService(self.oms)
        references = tuple(
            PaperInstrumentReference(
                item.instrument_id, item.market, item.reference_version
            )
            for item in plan.instruments
        )
        projector = PaperAccountingProjector(
            self.oms,
            self.state,
            PaperAccountingPolicy(initial_cash=self.settings.trading.initial_cash),
            references,
            plan.sessions,
            marks,
        )
        reader = (
            ExactPaperTargetReader(self.settings.research.artifact_root)
            if cycle.target_artifact_version is not None
            else None
        )
        executor = (
            PaperTargetExecutor(
                self.oms,
                service,
                {item.instrument_id: item.market for item in plan.instruments},
                {item.instrument_id: item.reference_price for item in cycle.market},
                {item.instrument_id: item.volume for item in cycle.market},
                _risk_policy(self.settings),
            )
            if reader is not None
            else None
        )
        runtime = PaperRuntime(
            self.oms,
            service,
            broker,
            self.state,
            scheduler,
            projector,
            self.settings.research.artifact_root,
            cycle.market,
            target_reader=reader,
            target_executor=executor,
            target_artifact_version=cycle.target_artifact_version,
        )
        result = runtime.run_once(cycle.session.isoformat(), cycle.as_of)
        replay_clean = True
        if plan.verify_duplicate_sessions and not result.safe_mode:
            before = _state_identity(self.oms, self.state, scheduler)
            duplicate = runtime.run_once(cycle.session.isoformat(), cycle.as_of)
            after = _state_identity(self.oms, self.state, scheduler)
            replay_clean = before == after and all(
                state == "completed" for _, state in duplicate.job_states
            )
        projection_version = (
            projector.project(cycle.as_of).projection_version if not result.safe_mode else None
        )
        return result, replay_clean, projection_version

    def _plan_checks(self, plan: PaperSoakPlan) -> tuple[tuple[str, str], ...]:
        return (
            ("plan_schema", "passed"),
            ("pinned_calendar", "passed"),
            ("timezone_aware", "passed"),
            ("exact_target_versions", "passed"),
            (
                "settlement_extension_present",
                "passed"
                if plan.cycles[-1].session == plan.sessions[-1]
                else "failed",
            ),
        )

    def _final_checks(
        self, plan: PaperSoakPlan, failed_reason: str | None
    ) -> tuple[tuple[str, str], ...]:
        portfolio = self.state.current_portfolio()
        orders = self.oms.list_orders()
        checks: list[tuple[str, str]] = [
            ("runtime_safe_mode", "failed" if failed_reason else "passed"),
            (
                "all_orders_terminal",
                "passed" if all(item.state in TERMINAL_STATES for item in orders) else "failed",
            ),
            (
                "outbox_drained",
                "passed" if not self.oms.pending_outbox() else "failed",
            ),
            (
                "reservations_released",
                "passed"
                if self.oms.reserved_cash() == 0
                and all(int(item["reserved_quantity"]) == 0 for item in self.oms.reservations())
                else "failed",
            ),
            (
                "valuation_complete",
                "passed" if portfolio and bool(portfolio["valuation_complete"]) else "failed",
            ),
            (
                "accounting_reconciled",
                "passed"
                if portfolio and Decimal(str(portfolio["reconciliation_residual"])) == 0
                else "failed",
            ),
        ]
        if plan.require_final_settlement:
            checks.append(
                (
                    "final_settlement_complete",
                    "passed"
                    if portfolio
                    and Decimal(str(portfolio["settlement_payables"])) == 0
                    and Decimal(str(portfolio["settlement_receivables"])) == 0
                    else "failed",
                )
            )
        return tuple(checks)


def _risk_policy(settings: AppSettings) -> PaperStrategyRiskPolicy:
    return PaperStrategyRiskPolicy(
        version=settings.paper.strategy_risk_policy_version,
        initial_cash=settings.trading.initial_cash,
        maximum_gross_exposure=settings.trading.maximum_gross_exposure,
        maximum_single_position=settings.trading.maximum_position_weight,
        estimated_adverse_slippage_bps=settings.paper.estimated_adverse_slippage_bps,
        minimum_cash_buffer=settings.paper.minimum_cash_buffer,
        maximum_turnover=settings.paper.maximum_turnover,
        maximum_volume_participation=settings.paper.maximum_volume_participation,
        maximum_position_count=settings.paper.maximum_position_count,
    )


def _state_identity(
    oms: SQLiteOMSRepository, state: OperationsStateStore, scheduler: PaperScheduler
) -> str:
    value = {
        "orders": [asdict(item) for item in oms.list_orders()],
        "fills": list(oms.fills()),
        "outbox": list(oms.outbox()),
        "portfolio": state.current_portfolio(),
        "target": state.current_target_snapshot(),
        "runs": list(scheduler.runs()),
    }
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _instrument(raw: object) -> SoakInstrument:
    if not isinstance(raw, dict) or raw.keys() != {
        "instrument_id",
        "market",
        "reference_version",
    }:
        raise ValueError("paper soak instrument schema is invalid")
    reference_version = require_exact_version(str(raw["reference_version"]))
    return SoakInstrument(
        str(raw["instrument_id"]),
        Market(str(raw["market"])),
        reference_version,
    )


def _cycle(raw: object) -> SoakCycle:
    if not isinstance(raw, dict) or raw.keys() != {
        "session",
        "as_of",
        "market",
        "target_artifact_version",
        "broker_participation_cap",
    }:
        raise ValueError("paper soak cycle schema is invalid")
    if not isinstance(raw["market"], list):
        raise ValueError("paper soak market events must be an array")
    market_raw = cast(list[dict[str, Any]], raw["market"])
    market: list[PaperMarketEvent] = []
    market_fields = {
        "instrument_id",
        "event_time",
        "reference_price",
        "volume",
        "suspended",
        "limit_locked",
    }
    for item in market_raw:
        if not isinstance(item, dict) or item.keys() != market_fields:
            raise ValueError("paper soak market event schema is invalid")
        if not isinstance(item["volume"], int) or isinstance(item["volume"], bool):
            raise ValueError("paper soak market volume must be an integer")
        if not isinstance(item["suspended"], bool) or not isinstance(
            item["limit_locked"], bool
        ):
            raise ValueError("paper soak market flags must be booleans")
        market.append(
            PaperMarketEvent(
                str(item["instrument_id"]),
                datetime.fromisoformat(str(item["event_time"])),
                _decimal(item["reference_price"], "paper soak reference price"),
                int(item["volume"]),
                item["suspended"],
                item["limit_locked"],
            )
        )
    target = raw["target_artifact_version"]
    if target is not None and not isinstance(target, str):
        raise ValueError("paper soak target version must be a string or null")
    if isinstance(target, str):
        require_exact_version(target)
    broker_participation_cap = _decimal(
        raw["broker_participation_cap"], "paper soak broker participation cap"
    )
    return SoakCycle(
        date.fromisoformat(str(raw["session"])),
        datetime.fromisoformat(str(raw["as_of"])),
        tuple(market),
        str(target) if target is not None else None,
        broker_participation_cap,
    )


def _decimal(raw: object, field: str) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise ValueError(f"{field} must be a decimal value")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a decimal value") from exc
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    return value
