"""Typed, versioned, deterministic backtest event journal."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class BacktestEventKind(StrEnum):
    RUN_STARTED = "backtest.run_started"
    SETTLEMENT_PROCESSED = "accounting.settlement_processed"
    CORPORATE_ACTION_APPLIED = "accounting.corporate_action_applied"
    MARKET_OPENED = "market.opened"
    MARKET_CLOSED = "market.closed"
    TARGET_POSITION_SET = "target_position.set"
    ORDER_INTENT_CREATED = "order_intent.created"
    RISK_DECIDED = "risk.decided"
    ORDER_RESERVED = "order.reserved"
    ORDER_RESERVATION_UPDATED = "order.reservation_updated"
    ORDER_RESERVATION_RELEASED = "order.reservation_released"
    FILL_RECEIVED = "fill.received"
    PORTFOLIO_SNAPSHOTTED = "portfolio.snapshotted"
    RECONCILIATION_COMPLETED = "reconciliation.completed"
    RUN_COMPLETED = "backtest.run_completed"


@dataclass(frozen=True, slots=True)
class BacktestEvent:
    event_id: str
    run_id: str
    sequence: int
    kind: BacktestEventKind
    occurred_at: datetime
    schema_version: int
    correlation_id: str
    causation_id: str | None
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("event occurred_at must be timezone-aware")
        if not self.event_id or not self.run_id or self.sequence < 1 or self.schema_version < 1:
            raise ValueError("event identity and version must be valid")

    def canonical_dict(self) -> dict[str, Any]:
        return asdict(self)


class DeterministicEventJournal:
    def __init__(self, run_id: str, schema_version: int = 1) -> None:
        if not run_id or schema_version < 1:
            raise ValueError("run id and schema version are required")
        self.run_id = run_id
        self.schema_version = schema_version
        self._events: list[BacktestEvent] = []

    @property
    def events(self) -> tuple[BacktestEvent, ...]:
        return tuple(self._events)

    def append(
        self,
        kind: BacktestEventKind,
        occurred_at: datetime,
        payload: dict[str, Any],
        *,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> BacktestEvent:
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("event occurred_at must be timezone-aware")
        if self._events and occurred_at < self._events[-1].occurred_at:
            raise ValueError("events must be appended in chronological order")
        sequence = len(self._events) + 1
        canonical_payload = _canonical(payload)
        resolved_correlation_id = correlation_id or f"{self.run_id}:{sequence}"
        identity = {
            "run_id": self.run_id,
            "sequence": sequence,
            "kind": kind.value,
            "occurred_at": occurred_at.isoformat(),
            "schema_version": self.schema_version,
            "correlation_id": resolved_correlation_id,
            "causation_id": causation_id,
            "payload": canonical_payload,
        }
        event_id = hashlib.sha256(_encoded(identity)).hexdigest()
        event = BacktestEvent(
            event_id=event_id,
            run_id=self.run_id,
            sequence=sequence,
            kind=kind,
            occurred_at=occurred_at,
            schema_version=self.schema_version,
            correlation_id=resolved_correlation_id,
            causation_id=causation_id,
            payload=canonical_payload,
        )
        self._events.append(event)
        return event

    def checksum(self) -> str:
        payload = [event.canonical_dict() for event in self._events]
        return hashlib.sha256(_encoded(payload)).hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    return value


def _encoded(value: Any) -> bytes:
    return json.dumps(
        _canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
