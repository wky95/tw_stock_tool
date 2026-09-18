"""Fail-closed deterministic replay for backtest event journals."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from island_quant.backtest.engine import instrument_from_payload
from island_quant.backtest.events import (
    BacktestEvent,
    BacktestEventKind,
    DeterministicEventJournal,
)
from island_quant.backtest.policies import MarkPolicy, SettlementPolicy
from island_quant.domain.models import Fill, Side
from island_quant.portfolio.accounting import CashDividend, PortfolioLedger, StockSplit


class ReplayMismatchError(RuntimeError):
    """Raised whenever the persisted event stream cannot be reproduced exactly."""


@dataclass(frozen=True, slots=True)
class ReplayResult:
    run_id: str
    event_checksum: str
    journal_checksum: str
    final_snapshot_checksum: str
    snapshot_count: int
    reconciliation_count: int


def replay_events(events: tuple[BacktestEvent, ...]) -> ReplayResult:
    """Rebuild accounting state and reject any identity or reconciliation mismatch."""
    if not events or events[0].kind is not BacktestEventKind.RUN_STARTED:
        raise ReplayMismatchError("event stream must begin with backtest.run_started")
    rebuilt = DeterministicEventJournal(events[0].run_id, events[0].schema_version)
    for expected_sequence, event in enumerate(events, start=1):
        if event.sequence != expected_sequence or event.run_id != events[0].run_id:
            raise ReplayMismatchError("event sequence or run id mismatch")
        try:
            recreated = rebuilt.append(
                event.kind,
                event.occurred_at,
                event.payload,
                correlation_id=event.correlation_id,
                causation_id=event.causation_id,
            )
        except (TypeError, ValueError) as exc:
            raise ReplayMismatchError("event stream ordering or schema is invalid") from exc
        if recreated != event:
            raise ReplayMismatchError(f"event identity mismatch at sequence {event.sequence}")
    if any(event.kind is BacktestEventKind.RUN_COMPLETED for event in events[:-1]):
        raise ReplayMismatchError("run completed must be terminal")

    start = events[0]
    payload = start.payload
    try:
        settlement_data = _mapping(payload["settlement_policy"])
        settlement_policy = SettlementPolicy(
            version=str(settlement_data["version"]),
            lag_sessions=int(settlement_data["lag_sessions"]),
            currency=str(settlement_data["currency"]),
            obligation_netting=str(settlement_data["obligation_netting"]),
        )
        mark_data = _mapping(payload["mark_policy"])
        mark_policy = MarkPolicy(
            version=str(mark_data["version"]),
            maximum_stale_sessions=int(mark_data["maximum_stale_sessions"]),
            purpose=str(mark_data["purpose"]),
        )
        calendar = tuple(date.fromisoformat(str(item)) for item in payload["trading_calendar"])
        ledger = PortfolioLedger(
            str(payload["portfolio_id"]),
            Decimal(str(payload["initial_cash"])),
            settlement_policy,
            start.occurred_at,
            mark_policy,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ReplayMismatchError("run-start payload is invalid") from exc

    snapshot_count = 0
    reconciliation_count = 0
    approved = 0
    rejected = 0
    fills = 0
    intents: dict[str, dict[str, Any]] = {}
    intent_event_ids: dict[str, str] = {}
    risk_outcomes: dict[str, str] = {}
    risk_event_ids: dict[str, str] = {}
    reservations: dict[str, tuple[int, str]] = {}
    fill_business_ids: set[str] = set()
    action_ids: set[str] = set()
    aged_close_times: set[datetime] = set()
    for event in events[1:]:
        try:
            if event.kind is BacktestEventKind.ORDER_INTENT_CREATED:
                intent_id = str(event.payload["intent_id"])
                if intent_id in intents:
                    raise ReplayMismatchError("duplicate order intent identity")
                intents[intent_id] = event.payload
                intent_event_ids[intent_id] = event.event_id
            elif event.kind is BacktestEventKind.SETTLEMENT_PROCESSED:
                processed = ledger.process_settlements(
                    date.fromisoformat(str(event.payload["date"])), event.occurred_at
                )
                _require_equal(list(processed), event.payload["processed"], event, "settlements")
            elif event.kind is BacktestEventKind.CORPORATE_ACTION_APPLIED:
                action_id = str(event.payload["action_id"])
                if action_id in action_ids or not event.payload.get("action_version"):
                    raise ReplayMismatchError("duplicate or unversioned corporate action")
                action_ids.add(action_id)
                _replay_action(ledger, event)
            elif event.kind in {
                BacktestEventKind.MARKET_OPENED,
                BacktestEventKind.MARKET_CLOSED,
            }:
                if (
                    event.kind is BacktestEventKind.MARKET_CLOSED
                    and event.occurred_at not in aged_close_times
                ):
                    close_keys = {
                        instrument_from_payload(_mapping(item.payload["instrument"])).key
                        for item in events
                        if item.kind is BacktestEventKind.MARKET_CLOSED
                        and item.occurred_at == event.occurred_at
                    }
                    ledger.age_unmarked_positions(close_keys)
                    aged_close_times.add(event.occurred_at)
                ledger.mark(
                    instrument_from_payload(_mapping(event.payload["instrument"])),
                    Decimal(str(event.payload["price"])),
                    event.occurred_at,
                    "session_open"
                    if event.kind is BacktestEventKind.MARKET_OPENED
                    else "session_close",
                )
            elif event.kind is BacktestEventKind.RISK_DECIDED:
                intent_id = str(event.payload["intent_id"])
                if intent_id not in intents or event.causation_id != intent_event_ids[intent_id]:
                    raise ReplayMismatchError("risk decision lacks its order intent")
                if intent_id in risk_outcomes:
                    raise ReplayMismatchError("order intent has multiple risk decisions")
                if event.payload["outcome"] == "approve":
                    approved += 1
                elif event.payload["outcome"] == "reject":
                    rejected += 1
                else:
                    raise ReplayMismatchError("unknown risk outcome")
                risk_outcomes[intent_id] = str(event.payload["outcome"])
                risk_event_ids[intent_id] = event.event_id
            elif event.kind is BacktestEventKind.ORDER_RESERVED:
                intent_id = str(event.payload["intent_id"])
                if (
                    risk_outcomes.get(intent_id) != "approve"
                    or intent_id in reservations
                    or event.causation_id != risk_event_ids[intent_id]
                ):
                    raise ReplayMismatchError("invalid or duplicate order reservation")
                reservations[intent_id] = (
                    int(event.payload["remaining_quantity"]),
                    str(event.payload["status"]),
                )
            elif event.kind is BacktestEventKind.FILL_RECEIVED:
                if event.payload["status"] in {"filled", "partial"}:
                    intent_id = str(event.payload["intent_id"])
                    if risk_outcomes.get(intent_id) != "approve":
                        raise ReplayMismatchError("fill references an unapproved order intent")
                    if event.causation_id != risk_event_ids[intent_id]:
                        raise ReplayMismatchError("fill does not reference its risk approval")
                    fill = _fill_from_payload(_mapping(event.payload["fill"]))
                    if fill.fill_id in fill_business_ids:
                        raise ReplayMismatchError("duplicate fill business identity")
                    remaining, status = reservations.get(intent_id, (0, "missing"))
                    if status != "active":
                        raise ReplayMismatchError("fill lacks an active reservation")
                    fill_business_ids.add(fill.fill_id)
                    if fill.quantity > remaining:
                        raise ReplayMismatchError("fill exceeds remaining order quantity")
                    if fill.client_order_id != intents[intent_id]["idempotency_key"]:
                        raise ReplayMismatchError("fill references a different order identity")
                    reservations[intent_id] = (remaining - fill.quantity, "awaiting_update")
                    pending = ledger.apply_fill(fill, event.occurred_at.date(), calendar)
                    _require_equal(
                        pending.due_date.isoformat(),
                        event.payload["settlement_due"],
                        event,
                        "settlement due date",
                    )
                    fills += 1
                elif event.payload["status"] == "unfilled":
                    intent_id = str(event.payload["intent_id"])
                    if risk_outcomes.get(intent_id) != "approve":
                        raise ReplayMismatchError("unfilled outcome references unapproved intent")
                    if event.causation_id != risk_event_ids[intent_id]:
                        raise ReplayMismatchError("unfilled outcome lacks its risk approval")
                else:
                    raise ReplayMismatchError("unknown fill status")
            elif event.kind is BacktestEventKind.ORDER_RESERVATION_UPDATED:
                intent_id = str(event.payload["intent_id"])
                remaining, status = reservations.get(intent_id, (0, "missing"))
                payload_remaining = int(event.payload["remaining_quantity"])
                if status != "awaiting_update" or payload_remaining != remaining:
                    raise ReplayMismatchError("reservation update does not match fill")
                expected_status = "filled" if remaining == 0 else "active"
                if event.payload["status"] != expected_status:
                    raise ReplayMismatchError("reservation update has invalid status")
                reservations[intent_id] = (remaining, expected_status)
            elif event.kind is BacktestEventKind.ORDER_RESERVATION_RELEASED:
                intent_id = str(event.payload["intent_id"])
                remaining, status = reservations.get(intent_id, (0, "missing"))
                if status != "active" or event.payload["status"] not in {
                    "cancelled",
                    "expired",
                    "rejected",
                }:
                    raise ReplayMismatchError("invalid reservation release")
                reservations[intent_id] = (remaining, str(event.payload["status"]))
            elif event.kind is BacktestEventKind.PORTFOLIO_SNAPSHOTTED:
                snapshot = ledger.snapshot(event.occurred_at)
                expected = {**asdict(snapshot), "journal_checksum": ledger.journal_checksum()}
                _require_equal(_canonical(expected), event.payload, event, "portfolio snapshot")
                snapshot_count += 1
            elif event.kind is BacktestEventKind.RECONCILIATION_COMPLETED:
                ledger.reconcile()
                snapshot = ledger.snapshot(event.occurred_at)
                expected = {
                    "status": "passed",
                    "snapshot_checksum": snapshot.checksum,
                    "journal_checksum": ledger.journal_checksum(),
                }
                _require_equal(expected, event.payload, event, "reconciliation")
                reconciliation_count += 1
            elif event.kind is BacktestEventKind.RUN_COMPLETED:
                if any(
                    status in {"active", "awaiting_update"} for _, status in reservations.values()
                ):
                    raise ReplayMismatchError("run completed with active order reservations")
                snapshot = ledger.snapshot(event.occurred_at)
                expected = {
                    "final_snapshot_checksum": snapshot.checksum,
                    "journal_checksum": ledger.journal_checksum(),
                    "approved_orders": approved,
                    "rejected_orders": rejected,
                    "fills": fills,
                }
                _require_equal(expected, event.payload, event, "run completion")
        except ReplayMismatchError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ReplayMismatchError(f"invalid payload at sequence {event.sequence}") from exc

    if events[-1].kind is not BacktestEventKind.RUN_COMPLETED:
        raise ReplayMismatchError("event stream must end with backtest.run_completed")
    final = ledger.snapshot(events[-1].occurred_at)
    return ReplayResult(
        events[0].run_id,
        rebuilt.checksum(),
        ledger.journal_checksum(),
        final.checksum,
        snapshot_count,
        reconciliation_count,
    )


def _replay_action(ledger: PortfolioLedger, event: BacktestEvent) -> None:
    payload = event.payload
    instrument = instrument_from_payload(_mapping(payload["instrument"]))
    if payload["action_type"] == "cash_dividend":
        dividend = CashDividend(
            action_id=str(payload["action_id"]),
            instrument=instrument,
            ex_date=date.fromisoformat(str(payload["ex_date"])),
            record_date=date.fromisoformat(str(payload["record_date"])),
            pay_date=date.fromisoformat(str(payload["pay_date"])),
            amount_per_share=Decimal(str(payload["amount_per_share"])),
            announcement_at=datetime.fromisoformat(str(payload["announcement_at"])),
            action_version=str(payload["action_version"]),
            policy_version=str(payload["policy_version"]),
        )
        accrued = ledger.accrue_dividend(dividend, event.occurred_at)
        _require_equal(str(accrued), payload["accrued_amount"], event, "dividend accrual")
    elif payload["action_type"] == "stock_split":
        split = StockSplit(
            action_id=str(payload["action_id"]),
            instrument=instrument,
            effective_date=date.fromisoformat(str(payload["effective_date"])),
            ratio=Decimal(str(payload["ratio"])),
            announcement_at=datetime.fromisoformat(str(payload["announcement_at"])),
            action_version=str(payload["action_version"]),
            policy_version=str(payload["policy_version"]),
        )
        old_quantity, new_quantity = ledger.apply_split(split, event.occurred_at)
        _require_equal(old_quantity, payload["old_quantity"], event, "pre-split quantity")
        _require_equal(new_quantity, payload["new_quantity"], event, "post-split quantity")
    else:
        raise ReplayMismatchError("unknown corporate action type")


def _fill_from_payload(payload: dict[str, Any]) -> Fill:
    return Fill(
        fill_id=str(payload["fill_id"]),
        client_order_id=str(payload["client_order_id"]),
        instrument=instrument_from_payload(_mapping(payload["instrument"])),
        side=Side(str(payload["side"])),
        quantity=int(payload["quantity"]),
        price=Decimal(str(payload["price"])),
        fee=Decimal(str(payload["fee"])),
        tax=Decimal(str(payload["tax"])),
        event_time=datetime.fromisoformat(str(payload["event_time"])),
        ingestion_time=datetime.fromisoformat(str(payload["ingestion_time"])),
    )


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("expected mapping")
    return value


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _require_equal(
    actual: object, expected: object, event: BacktestEvent, description: str
) -> None:
    if actual != expected:
        raise ReplayMismatchError(
            f"{description} mismatch at sequence {event.sequence}: {actual!r} != {expected!r}"
        )
