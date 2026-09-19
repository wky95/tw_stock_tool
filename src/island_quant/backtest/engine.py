"""Deterministic event-driven engine for long-only Taiwan cash-equity fixtures."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from island_quant.backtest.contracts import BacktestSession, BacktestStrategy
from island_quant.backtest.events import (
    BacktestEvent,
    BacktestEventKind,
    DeterministicEventJournal,
)
from island_quant.backtest.policies import (
    FeeTaxPolicy,
    FillPolicy,
    MarkPolicy,
    RiskPolicy,
    SettlementPolicy,
    TickSizePolicy,
)
from island_quant.domain.models import Instrument, Market, RiskOutcome, Side
from island_quant.execution.simulation import ConservativeFillSimulator, TargetOrderConverter
from island_quant.portfolio.accounting import (
    AccountingSnapshot,
    CashDividend,
    PortfolioLedger,
    StockSplit,
)
from island_quant.risk.pretrade import PreTradeRiskEngine
from island_quant.risk.reservations import PendingOrderBook, ReservationStatus

TAIPEI = ZoneInfo("Asia/Taipei")
CorporateAction = CashDividend | StockSplit


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    run_id: str
    portfolio_id: str
    initial_cash: Decimal
    trading_calendar: tuple[date, ...]
    fee_policy: FeeTaxPolicy
    settlement_policy: SettlementPolicy
    fill_policy: FillPolicy
    risk_policy: RiskPolicy
    mark_policy: MarkPolicy = field(default_factory=MarkPolicy)
    tick_policy: TickSizePolicy = field(default_factory=TickSizePolicy)
    event_schema_version: int = 1
    pit_reference_complete: bool = False

    def __post_init__(self) -> None:
        if not self.run_id or not self.portfolio_id or self.initial_cash <= 0:
            raise ValueError("backtest identity and initial cash are required")
        if tuple(sorted(set(self.trading_calendar))) != self.trading_calendar:
            raise ValueError("trading calendar must be sorted and unique")
        if not self.trading_calendar:
            raise ValueError("trading calendar cannot be empty")


@dataclass(frozen=True, slots=True)
class BacktestResult:
    config: BacktestConfig
    strategy_id: str
    strategy_version: str
    events: tuple[BacktestEvent, ...]
    snapshots: tuple[AccountingSnapshot, ...]
    final_snapshot: AccountingSnapshot
    event_checksum: str
    journal_checksum: str
    approved_orders: int
    rejected_orders: int
    fills: int
    manifest: BacktestManifest


@dataclass(frozen=True, slots=True)
class BacktestManifest:
    execution_sessions: tuple[date, ...]
    settlement_extension_sessions: tuple[date, ...]
    policy_versions: tuple[tuple[str, str], ...]


class EventDrivenBacktestEngine:
    def run(
        self,
        config: BacktestConfig,
        sessions: tuple[BacktestSession, ...],
        strategy: BacktestStrategy,
        corporate_actions: tuple[CorporateAction, ...] = (),
    ) -> BacktestResult:
        self._validate_inputs(config, sessions, corporate_actions)
        first_time = sessions[0].decision_time - timedelta(minutes=1)
        journal = DeterministicEventJournal(config.run_id, config.event_schema_version)
        ledger = PortfolioLedger(
            config.portfolio_id,
            config.initial_cash,
            config.settlement_policy,
            first_time,
            config.mark_policy,
        )
        journal.append(
            BacktestEventKind.RUN_STARTED,
            first_time,
            {
                "portfolio_id": config.portfolio_id,
                "initial_cash": config.initial_cash,
                "trading_calendar": [day.isoformat() for day in config.trading_calendar],
                "fee_policy": asdict(config.fee_policy),
                "settlement_policy": asdict(config.settlement_policy),
                "fill_policy": asdict(config.fill_policy),
                "risk_policy": asdict(config.risk_policy),
                "mark_policy": asdict(config.mark_policy),
                "tick_policy": asdict(config.tick_policy),
                "strategy_id": strategy.strategy_id,
                "strategy_version": strategy.version,
                "pit_reference_complete": config.pit_reference_complete,
            },
        )
        converter = TargetOrderConverter(config.fee_policy)
        risk = PreTradeRiskEngine(config.risk_policy, config.fee_policy)
        simulator = ConservativeFillSimulator(
            config.fill_policy, config.fee_policy, config.tick_policy
        )
        pending_orders = PendingOrderBook(config.fee_policy)
        action_map = self._action_map(corporate_actions)
        snapshots: list[AccountingSnapshot] = []
        approved = 0
        rejected = 0
        fill_count = 0
        final_execution_date = sessions[-1].trade_date
        sessions_by_date = {session.trade_date: session for session in sessions}
        for calendar_date in config.trading_calendar:
            if calendar_date > config.settlement_policy.due_date(
                final_execution_date, config.trading_calendar
            ):
                break
            session = sessions_by_date.get(calendar_date)
            intents = []
            if session is not None:
                predecision = ledger.snapshot(session.decision_time)
                targets = strategy.targets(session)
                target_keys = [target.instrument.key for target in targets]
                if len(target_keys) != len(set(target_keys)):
                    raise ValueError("strategy emitted duplicate instrument targets")
                session_keys = {quote.instrument.key for quote in session.quotes}
                if any(
                    target.generated_at != session.decision_time
                    or target.instrument.key not in session_keys
                    or target.strategy_id != strategy.strategy_id
                    for target in targets
                ):
                    raise ValueError("strategy target violates pinned decision contract")
                for target in sorted(targets, key=lambda item: item.instrument.key):
                    target_event = journal.append(
                        BacktestEventKind.TARGET_POSITION_SET,
                        target.generated_at,
                        {
                            "strategy_id": target.strategy_id,
                            "instrument": _instrument_payload(target.instrument),
                            "target_weight": target.target_weight,
                            "reason": target.reason,
                        },
                    )
                    intent = converter.convert(
                        target,
                        portfolio_id=config.portfolio_id,
                        net_asset_value=predecision.net_asset_value,
                        reference_price=session.decision_price(target.instrument),
                        current_quantity=ledger.quantity(target.instrument),
                        run_id=config.run_id,
                    )
                    if intent is None:
                        continue
                    intent_event = journal.append(
                        BacktestEventKind.ORDER_INTENT_CREATED,
                        intent.created_at,
                        _intent_payload(intent, converter.version),
                        causation_id=target_event.event_id,
                    )
                    intents.append((intent, intent_event.event_id, target.reason))
            accounting_time = datetime.combine(calendar_date, time(8, 30), tzinfo=TAIPEI)
            processed = ledger.process_settlements(calendar_date, accounting_time)
            journal.append(
                BacktestEventKind.SETTLEMENT_PROCESSED,
                accounting_time,
                {"date": calendar_date.isoformat(), "processed": processed},
            )
            for action in action_map.get(calendar_date, ()):
                payload = self._apply_action(ledger, action, accounting_time)
                journal.append(BacktestEventKind.CORPORATE_ACTION_APPLIED, accounting_time, payload)
            if session is None:
                continue
            quotes = {quote.instrument.key: quote for quote in session.quotes}
            for quote in sorted(session.quotes, key=lambda item: item.instrument.key):
                ledger.mark(
                    quote.instrument, quote.open_price, session.execution_time, "session_open"
                )
                journal.append(
                    BacktestEventKind.MARKET_OPENED,
                    session.execution_time,
                    _quote_payload(quote, "open"),
                )
            open_snapshot = ledger.snapshot(session.execution_time)
            intents.sort(
                key=lambda item: (
                    0 if item[0].side is Side.SELL else 1,
                    item[0].instrument.key,
                )
            )
            for intent, intent_event_id, target_reason in intents:
                quote = quotes[intent.instrument.key]
                estimated_price = simulator.estimated_price(intent, quote)
                decision = risk.evaluate(
                    intent,
                    estimated_price=estimated_price,
                    ledger=ledger,
                    net_asset_value=open_snapshot.net_asset_value,
                    evaluated_at=session.execution_time,
                    pending_orders=pending_orders,
                )
                risk_event = journal.append(
                    BacktestEventKind.RISK_DECIDED,
                    session.execution_time,
                    {
                        "intent_id": str(intent.intent_id),
                        "outcome": decision.outcome.value,
                        "reasons": decision.reasons,
                        "inputs": decision.inputs,
                        "rule_version": decision.rule_version,
                    },
                    causation_id=intent_event_id,
                )
                if decision.outcome is RiskOutcome.REJECT:
                    rejected += 1
                    continue
                approved += 1
                reservation = pending_orders.reserve(intent, estimated_price)
                journal.append(
                    BacktestEventKind.ORDER_RESERVED,
                    session.execution_time,
                    _reservation_payload(reservation),
                    causation_id=risk_event.event_id,
                )
                outcome = simulator.simulate(intent, quote, run_id=config.run_id)
                if outcome.fill is None:
                    journal.append(
                        BacktestEventKind.FILL_RECEIVED,
                        session.execution_time,
                        {
                            "status": "unfilled",
                            "intent_id": str(intent.intent_id),
                            "reason": outcome.reason,
                            "unfilled_quantity": outcome.unfilled_quantity,
                        },
                        causation_id=risk_event.event_id,
                    )
                    released = pending_orders.release(
                        str(intent.intent_id), ReservationStatus.EXPIRED
                    )
                    journal.append(
                        BacktestEventKind.ORDER_RESERVATION_RELEASED,
                        session.execution_time,
                        _reservation_payload(released),
                        causation_id=risk_event.event_id,
                    )
                    continue
                fill = outcome.fill
                updated_reservation = pending_orders.apply_fill(str(intent.intent_id), fill)
                pending = ledger.apply_fill(fill, session.trade_date, config.trading_calendar)
                fill_count += 1
                journal.append(
                    BacktestEventKind.FILL_RECEIVED,
                    fill.event_time,
                    {
                        "status": "filled" if not outcome.unfilled_quantity else "partial",
                        "intent_id": str(intent.intent_id),
                        "fill": _fill_payload(fill),
                        "settlement_due": pending.due_date.isoformat(),
                        "unfilled_quantity": outcome.unfilled_quantity,
                        "fill_policy_version": config.fill_policy.version,
                        "fee_policy_version": config.fee_policy.version,
                        "reference_price": quote.open_price,
                        "reference_source": "next_session_open",
                        "tick_policy_version": config.tick_policy.version,
                        "target_reason": target_reason,
                    },
                    causation_id=risk_event.event_id,
                )
                journal.append(
                    BacktestEventKind.ORDER_RESERVATION_UPDATED,
                    fill.event_time,
                    _reservation_payload(updated_reservation),
                    causation_id=risk_event.event_id,
                )
                if outcome.unfilled_quantity and config.fill_policy.cancel_unfilled_after_session:
                    released = pending_orders.release(
                        str(intent.intent_id), ReservationStatus.CANCELLED
                    )
                    journal.append(
                        BacktestEventKind.ORDER_RESERVATION_RELEASED,
                        fill.event_time,
                        _reservation_payload(released),
                        causation_id=risk_event.event_id,
                    )
            marked_keys = {quote.instrument.key for quote in session.quotes}
            ledger.age_unmarked_positions(marked_keys)
            for quote in sorted(session.quotes, key=lambda item: item.instrument.key):
                ledger.mark(
                    quote.instrument, quote.close_price, session.close_time, "session_close"
                )
                journal.append(
                    BacktestEventKind.MARKET_CLOSED,
                    session.close_time,
                    _quote_payload(quote, "close"),
                )
            snapshot = ledger.snapshot(session.close_time)
            snapshots.append(snapshot)
            journal.append(
                BacktestEventKind.PORTFOLIO_SNAPSHOTTED,
                session.close_time,
                _snapshot_payload(snapshot, ledger.journal_checksum()),
            )
            journal.append(
                BacktestEventKind.RECONCILIATION_COMPLETED,
                session.close_time,
                {
                    "status": "passed",
                    "snapshot_checksum": snapshot.checksum,
                    "journal_checksum": ledger.journal_checksum(),
                },
            )
        final_time = datetime.combine(
            config.settlement_policy.due_date(final_execution_date, config.trading_calendar),
            time(17),
            tzinfo=TAIPEI,
        )
        final_snapshot = ledger.snapshot(final_time)
        journal.append(
            BacktestEventKind.RUN_COMPLETED,
            final_time,
            {
                "final_snapshot_checksum": final_snapshot.checksum,
                "journal_checksum": ledger.journal_checksum(),
                "approved_orders": approved,
                "rejected_orders": rejected,
                "fills": fill_count,
            },
        )
        final_due = config.settlement_policy.due_date(final_execution_date, config.trading_calendar)
        final_index = config.trading_calendar.index(final_execution_date)
        due_index = config.trading_calendar.index(final_due)
        manifest = BacktestManifest(
            tuple(session.trade_date for session in sessions),
            config.trading_calendar[final_index + 1 : due_index + 1],
            (
                ("fee_tax", config.fee_policy.version),
                ("settlement", config.settlement_policy.version),
                ("fill", config.fill_policy.version),
                ("risk", config.risk_policy.version),
                ("mark", config.mark_policy.version),
                ("tick", config.tick_policy.version),
            ),
        )
        return BacktestResult(
            config,
            strategy.strategy_id,
            strategy.version,
            journal.events,
            tuple(snapshots),
            final_snapshot,
            journal.checksum(),
            ledger.journal_checksum(),
            approved,
            rejected,
            fill_count,
            manifest,
        )

    @staticmethod
    def _validate_inputs(
        config: BacktestConfig,
        sessions: tuple[BacktestSession, ...],
        actions: tuple[CorporateAction, ...],
    ) -> None:
        if not sessions:
            raise ValueError("backtest sessions cannot be empty")
        dates = tuple(session.trade_date for session in sessions)
        if tuple(sorted(set(dates))) != dates:
            raise ValueError("backtest sessions must be sorted and unique")
        if not set(dates).issubset(config.trading_calendar):
            raise ValueError("backtest session is absent from the pinned calendar")
        final_index = config.trading_calendar.index(dates[-1])
        if final_index + config.settlement_policy.lag_sessions >= len(config.trading_calendar):
            raise ValueError("calendar must extend through final settlement")
        action_ids = [action.action_id for action in actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("corporate action ids must be unique")
        final_due = config.settlement_policy.due_date(dates[-1], config.trading_calendar)
        calendar_days = set(config.trading_calendar)
        for action in actions:
            effective = (
                action.ex_date if isinstance(action, CashDividend) else action.effective_date
            )
            if effective not in calendar_days or not dates[0] <= effective <= final_due:
                raise ValueError("corporate action is outside the replayable backtest window")

    @staticmethod
    def _action_map(
        actions: tuple[CorporateAction, ...],
    ) -> dict[date, tuple[CorporateAction, ...]]:
        grouped: dict[date, list[CorporateAction]] = {}
        for action in actions:
            effective = (
                action.ex_date if isinstance(action, CashDividend) else action.effective_date
            )
            grouped.setdefault(effective, []).append(action)
        return {
            day: tuple(sorted(items, key=lambda item: item.action_id))
            for day, items in grouped.items()
        }

    @staticmethod
    def _apply_action(
        ledger: PortfolioLedger, action: CorporateAction, occurred_at: datetime
    ) -> dict[str, Any]:
        if isinstance(action, CashDividend):
            entitled_quantity = ledger.quantity(action.instrument)
            amount = ledger.accrue_dividend(action, occurred_at)
            return {
                "action_type": "cash_dividend",
                "action_id": action.action_id,
                "instrument": _instrument_payload(action.instrument),
                "ex_date": action.ex_date.isoformat(),
                "record_date": action.record_date.isoformat(),
                "pay_date": action.pay_date.isoformat(),
                "announcement_at": action.announcement_at,
                "action_version": action.action_version,
                "amount_per_share": action.amount_per_share,
                "entitled_quantity": entitled_quantity,
                "current_quantity": ledger.quantity(action.instrument),
                "accrued_amount": amount,
                "policy_version": action.policy_version,
            }
        old_quantity, new_quantity = ledger.apply_split(action, occurred_at)
        return {
            "action_type": "stock_split",
            "action_id": action.action_id,
            "instrument": _instrument_payload(action.instrument),
            "effective_date": action.effective_date.isoformat(),
            "announcement_at": action.announcement_at,
            "action_version": action.action_version,
            "ratio": action.ratio,
            "old_quantity": old_quantity,
            "new_quantity": new_quantity,
            "policy_version": action.policy_version,
        }


def _instrument_payload(instrument: Instrument) -> dict[str, Any]:
    return {
        "symbol": instrument.symbol,
        "market": instrument.market.value,
        "asset_type": instrument.asset_type.value,
        "currency": instrument.currency,
        "lot_size": instrument.lot_size,
        "price_tick": instrument.price_tick,
    }


def instrument_from_payload(payload: dict[str, Any]) -> Instrument:
    from island_quant.domain.models import AssetType

    return Instrument(
        symbol=str(payload["symbol"]),
        market=Market(payload["market"]),
        asset_type=AssetType(payload["asset_type"]),
        currency=str(payload["currency"]),
        lot_size=int(payload["lot_size"]),
        price_tick=Decimal(str(payload["price_tick"])),
    )


def _intent_payload(intent: Any, converter_version: str) -> dict[str, Any]:
    return {
        "intent_id": str(intent.intent_id),
        "instrument": _instrument_payload(intent.instrument),
        "side": intent.side.value,
        "quantity": intent.quantity,
        "order_type": intent.order_type.value,
        "idempotency_key": intent.idempotency_key,
        "converter_version": converter_version,
    }


def _quote_payload(quote: Any, field: str) -> dict[str, Any]:
    return {
        "instrument": _instrument_payload(quote.instrument),
        "field": field,
        "price": quote.open_price if field == "open" else quote.close_price,
        "volume": quote.volume,
        "suspended": quote.suspended,
        "limit_locked": quote.limit_locked,
    }


def _fill_payload(fill: Any) -> dict[str, Any]:
    return {
        "fill_id": fill.fill_id,
        "client_order_id": fill.client_order_id,
        "instrument": _instrument_payload(fill.instrument),
        "side": fill.side.value,
        "quantity": fill.quantity,
        "price": fill.price,
        "fee": fill.fee,
        "tax": fill.tax,
        "event_time": fill.event_time,
        "ingestion_time": fill.ingestion_time,
    }


def _reservation_payload(reservation: Any) -> dict[str, Any]:
    return {
        "intent_id": reservation.intent_id,
        "idempotency_key": reservation.idempotency_key,
        "instrument_key": reservation.instrument_key,
        "side": reservation.side.value,
        "original_quantity": reservation.original_quantity,
        "remaining_quantity": reservation.remaining_quantity,
        "estimated_price": reservation.estimated_price,
        "reserved_cash": reservation.reserved_cash,
        "cumulative_fill_gross": reservation.cumulative_fill_gross,
        "commission_charged": reservation.commission_charged,
        "status": reservation.status.value,
    }


def _snapshot_payload(snapshot: AccountingSnapshot, journal_checksum: str) -> dict[str, Any]:
    return {
        **asdict(snapshot),
        "journal_checksum": journal_checksum,
    }
