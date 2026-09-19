"""Storage-neutral persistent OMS contracts and fail-closed state machine."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from island_quant.pipeline.artifacts import canonical_json


class OMSState(StrEnum):
    CREATED = "Created"
    RISK_PENDING = "RiskPending"
    RISK_APPROVED = "RiskApproved"
    RISK_REJECTED = "RiskRejected"
    SUBMIT_PENDING = "SubmitPending"
    SUBMITTED = "Submitted"
    ACKNOWLEDGED = "Acknowledged"
    PARTIALLY_FILLED = "PartiallyFilled"
    FILLED = "Filled"
    CANCEL_PENDING = "CancelPending"
    CANCELLED = "Cancelled"
    REPLACE_PENDING = "ReplacePending"
    REPLACED = "Replaced"
    REJECTED = "Rejected"
    EXPIRED = "Expired"
    UNKNOWN = "Unknown"


TERMINAL_STATES = {
    OMSState.RISK_REJECTED,
    OMSState.FILLED,
    OMSState.CANCELLED,
    OMSState.REJECTED,
    OMSState.EXPIRED,
}

ALLOWED_TRANSITIONS: dict[OMSState, frozenset[OMSState]] = {
    OMSState.CREATED: frozenset({OMSState.RISK_PENDING}),
    OMSState.RISK_PENDING: frozenset({OMSState.RISK_APPROVED, OMSState.RISK_REJECTED}),
    OMSState.RISK_APPROVED: frozenset({OMSState.SUBMIT_PENDING}),
    OMSState.RISK_REJECTED: frozenset(),
    OMSState.SUBMIT_PENDING: frozenset({OMSState.SUBMITTED, OMSState.REJECTED, OMSState.UNKNOWN}),
    OMSState.SUBMITTED: frozenset(
        {
            OMSState.ACKNOWLEDGED,
            OMSState.REJECTED,
            OMSState.CANCEL_PENDING,
            OMSState.UNKNOWN,
        }
    ),
    OMSState.ACKNOWLEDGED: frozenset(
        {
            OMSState.PARTIALLY_FILLED,
            OMSState.FILLED,
            OMSState.CANCEL_PENDING,
            OMSState.REPLACE_PENDING,
            OMSState.REJECTED,
            OMSState.EXPIRED,
            OMSState.UNKNOWN,
        }
    ),
    OMSState.PARTIALLY_FILLED: frozenset(
        {
            OMSState.PARTIALLY_FILLED,
            OMSState.FILLED,
            OMSState.CANCEL_PENDING,
            OMSState.REPLACE_PENDING,
            OMSState.EXPIRED,
            OMSState.UNKNOWN,
        }
    ),
    OMSState.CANCEL_PENDING: frozenset(
        {OMSState.CANCELLED, OMSState.PARTIALLY_FILLED, OMSState.FILLED, OMSState.UNKNOWN}
    ),
    OMSState.REPLACE_PENDING: frozenset(
        {OMSState.REPLACED, OMSState.PARTIALLY_FILLED, OMSState.FILLED, OMSState.UNKNOWN}
    ),
    OMSState.REPLACED: frozenset({OMSState.SUBMIT_PENDING, OMSState.CANCEL_PENDING}),
    OMSState.FILLED: frozenset(),
    OMSState.CANCELLED: frozenset(),
    OMSState.REJECTED: frozenset(),
    OMSState.EXPIRED: frozenset(),
    OMSState.UNKNOWN: frozenset(
        {OMSState.SUBMITTED, OMSState.ACKNOWLEDGED, OMSState.CANCELLED, OMSState.REJECTED}
    ),
}


class OMSInvariantError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OMSOrder:
    order_id: str
    client_order_id: str
    idempotency_key: str
    instrument_id: str
    side: str
    quantity: int
    filled_quantity: int
    limit_price: Decimal | None
    state: OMSState
    version: int
    sequence: int
    environment: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.environment != "paper":
            raise ValueError("OMS order environment must be paper")
        if self.quantity < 1 or not 0 <= self.filled_quantity <= self.quantity:
            raise ValueError("OMS order quantities are invalid")
        if self.side not in {"buy", "sell"}:
            raise ValueError("OMS side is invalid")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("OMS timestamps must be timezone-aware")


@dataclass(frozen=True, slots=True)
class OMSEvent:
    event_id: str
    schema_version: int
    order_id: str
    client_order_id: str
    idempotency_key: str
    correlation_id: str
    causation_id: str | None
    expected_previous_state: OMSState | None
    state: OMSState
    event_time: datetime
    received_time: datetime
    sequence: int
    reason_code: str
    business_identity: str
    payload: tuple[tuple[str, str], ...]
    payload_checksum: str

    @classmethod
    def create(
        cls,
        *,
        order_id: str,
        client_order_id: str,
        idempotency_key: str,
        correlation_id: str,
        causation_id: str | None,
        expected_previous_state: OMSState | None,
        state: OMSState,
        event_time: datetime,
        received_time: datetime,
        sequence: int,
        reason_code: str,
        business_identity: str,
        payload: dict[str, object] | None = None,
        schema_version: int = 1,
    ) -> OMSEvent:
        if event_time.tzinfo is None or received_time.tzinfo is None:
            raise ValueError("OMS event timestamps must be timezone-aware")
        if received_time < event_time or sequence < 1:
            raise ValueError("OMS event ordering is invalid")
        normalized = tuple(sorted((str(key), str(value)) for key, value in (payload or {}).items()))
        checksum = hashlib.sha256(canonical_json(normalized)).hexdigest()
        identity = canonical_json(
            {
                "order_id": order_id,
                "state": state.value,
                "sequence": sequence,
                "business_identity": business_identity,
                "payload_checksum": checksum,
            }
        )
        event_id = hashlib.sha256(identity).hexdigest()
        return cls(
            event_id,
            schema_version,
            order_id,
            client_order_id,
            idempotency_key,
            correlation_id,
            causation_id,
            expected_previous_state,
            state,
            event_time,
            received_time,
            sequence,
            reason_code,
            business_identity,
            normalized,
            checksum,
        )

    def verify(self) -> None:
        if hashlib.sha256(canonical_json(self.payload)).hexdigest() != self.payload_checksum:
            raise OMSInvariantError("OMS event payload checksum mismatch")
        identity = canonical_json(
            {
                "order_id": self.order_id,
                "state": self.state.value,
                "sequence": self.sequence,
                "business_identity": self.business_identity,
                "payload_checksum": self.payload_checksum,
            }
        )
        if hashlib.sha256(identity).hexdigest() != self.event_id:
            raise OMSInvariantError("OMS event identity checksum mismatch")


def validate_transition(previous: OMSState, following: OMSState) -> None:
    if following not in ALLOWED_TRANSITIONS[previous]:
        raise OMSInvariantError(f"illegal OMS transition: {previous.value}->{following.value}")
