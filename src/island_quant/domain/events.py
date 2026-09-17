"""Replayable event envelope used across backtest, paper, and live modes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class EventKind(StrEnum):
    BAR_AVAILABLE = "bar.available"
    SIGNAL_GENERATED = "signal.generated"
    TARGET_POSITION_SET = "target_position.set"
    ORDER_INTENT_CREATED = "order_intent.created"
    RISK_DECIDED = "risk.decided"
    ORDER_STATE_CHANGED = "order.state_changed"
    FILL_RECEIVED = "fill.received"
    PORTFOLIO_SNAPSHOTTED = "portfolio.snapshotted"
    RECONCILIATION_COMPLETED = "reconciliation.completed"
    KILL_SWITCH_CHANGED = "kill_switch.changed"


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """Immutable journal record; payload holds serialized domain data."""

    kind: EventKind
    occurred_at: datetime
    recorded_at: datetime
    aggregate_id: str
    payload: dict[str, Any]
    sequence: int
    event_id: UUID = field(default_factory=uuid4)
    correlation_id: UUID = field(default_factory=uuid4)
    causation_id: UUID | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in ("occurred_at", "recorded_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.recorded_at < self.occurred_at:
            raise ValueError("recorded_at cannot precede occurred_at")
        if not self.aggregate_id or self.sequence < 0 or self.schema_version <= 0:
            raise ValueError("event identity, sequence, and schema version must be valid")

