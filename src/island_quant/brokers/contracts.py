"""Provider-neutral broker boundary for future external adapters.

The contracts in this module contain no vendor SDK types and perform no I/O.  An
adapter must reconcile an unknown command outcome before it may retry a command
which could create or increase market risk.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from island_quant.domain.models import OrderType, Side, TimeInForce


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _present(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


class BrokerCapability(StrEnum):
    SUBMIT = "submit"
    CANCEL = "cancel"
    REPLACE = "replace"
    QUERY_ORDER = "query_order"
    ORDER_CALLBACKS = "order_callbacks"
    FILL_CALLBACKS = "fill_callbacks"
    RECONCILIATION = "reconciliation"
    RATE_LIMIT_STATUS = "rate_limit_status"
    COMMON_LOT = "common_lot"
    INTRADAY_ODD_LOT = "intraday_odd_lot"
    AFTER_HOURS_ODD_LOT = "after_hours_odd_lot"
    CASH_LONG_ONLY = "cash_long_only"


class BrokerOrderState(StrEnum):
    PENDING_SUBMIT = "pending_submit"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REPLACE_PENDING = "replace_pending"
    REPLACED = "replaced"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class BrokerEventKind(StrEnum):
    ORDER = "order"
    FILL = "fill"
    SESSION = "session"


class CommandOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class SessionHealthState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    EXPIRED = "expired"
    FAILED = "failed"


class BrokerErrorCategory(StrEnum):
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    VALIDATION = "validation"
    UNSUPPORTED = "unsupported"
    RATE_LIMITED = "rate_limited"
    SESSION_EXPIRED = "session_expired"
    TRANSPORT = "transport"
    TIMEOUT = "timeout"
    BROKER_REJECTED = "broker_rejected"
    EXCHANGE_REJECTED = "exchange_rejected"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    CORRUPTED_PAYLOAD = "corrupted_payload"
    SEQUENCE_GAP = "sequence_gap"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


class RetryDirective(StrEnum):
    NEVER = "never"
    BACKOFF = "backoff"
    RECONCILE_THEN_DECIDE = "reconcile_then_decide"
    OPERATOR_DECISION = "operator_decision"


class OrderLot(StrEnum):
    COMMON = "common"
    INTRADAY_ODD = "intraday_odd"
    AFTER_HOURS_ODD = "after_hours_odd"


@dataclass(frozen=True, slots=True)
class CredentialReference:
    """Opaque lookup reference; deliberately cannot carry a secret value."""

    provider: str
    reference_name: str
    account_alias: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.provider, "provider"),
            (self.reference_name, "reference_name"),
            (self.account_alias, "account_alias"),
        ):
            _present(value, name)
            if "\n" in value or "\r" in value:
                raise ValueError(f"{name} must be a single-line reference")


@dataclass(frozen=True, slots=True)
class RateLimit:
    scope: str
    limit: int
    window_seconds: int
    remaining: int | None = None
    resets_at: datetime | None = None

    def __post_init__(self) -> None:
        _present(self.scope, "scope")
        if self.limit <= 0 or self.window_seconds <= 0:
            raise ValueError("rate limit and window must be positive")
        if self.remaining is not None and not 0 <= self.remaining <= self.limit:
            raise ValueError("rate-limit remaining is outside the limit")
        if self.resets_at is not None:
            _aware(self.resets_at, "resets_at")


@dataclass(frozen=True, slots=True)
class BrokerCapabilities:
    provider: str
    adapter_version: str
    broker_api_version: str
    capabilities: frozenset[BrokerCapability]
    rate_limits: tuple[RateLimit, ...] = ()

    def __post_init__(self) -> None:
        _present(self.provider, "provider")
        _present(self.adapter_version, "adapter_version")
        _present(self.broker_api_version, "broker_api_version")

    def require(self, capability: BrokerCapability) -> None:
        if capability not in self.capabilities:
            raise UnsupportedBrokerCapability(capability)


@dataclass(frozen=True, slots=True)
class BrokerError:
    schema_version: int
    category: BrokerErrorCategory
    code: str
    message: str
    retry: RetryDirective
    provider_code: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version <= 0:
            raise ValueError("broker error schema_version must be positive")
        _present(self.code, "code")
        _present(self.message, "message")
        if self.category in {BrokerErrorCategory.TIMEOUT, BrokerErrorCategory.TRANSPORT} and (
            self.retry is not RetryDirective.RECONCILE_THEN_DECIDE
        ):
            raise ValueError("ambiguous transport errors must reconcile before retry")


class UnsupportedBrokerCapability(RuntimeError):
    def __init__(self, capability: BrokerCapability) -> None:
        super().__init__(f"broker capability is unsupported: {capability.value}")
        self.capability = capability


@dataclass(frozen=True, slots=True)
class SubmitOrderRequest:
    client_order_id: str
    idempotency_key: str
    correlation_id: str
    causation_id: str
    instrument_id: str
    side: Side
    quantity: int
    order_type: OrderType
    time_in_force: TimeInForce
    order_lot: OrderLot
    submitted_at: datetime
    limit_price: Decimal | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.client_order_id, "client_order_id"),
            (self.idempotency_key, "idempotency_key"),
            (self.correlation_id, "correlation_id"),
            (self.causation_id, "causation_id"),
            (self.instrument_id, "instrument_id"),
        ):
            _present(value, name)
        _aware(self.submitted_at, "submitted_at")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit order requires limit_price")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price must be positive")


@dataclass(frozen=True, slots=True)
class CancelOrderRequest:
    client_order_id: str
    correlation_id: str
    causation_id: str
    requested_at: datetime

    def __post_init__(self) -> None:
        _present(self.client_order_id, "client_order_id")
        _present(self.correlation_id, "correlation_id")
        _present(self.causation_id, "causation_id")
        _aware(self.requested_at, "requested_at")


@dataclass(frozen=True, slots=True)
class ReplaceOrderRequest:
    client_order_id: str
    correlation_id: str
    causation_id: str
    requested_at: datetime
    new_quantity: int
    new_limit_price: Decimal | None

    def __post_init__(self) -> None:
        _present(self.client_order_id, "client_order_id")
        _present(self.correlation_id, "correlation_id")
        _present(self.causation_id, "causation_id")
        _aware(self.requested_at, "requested_at")
        if self.new_quantity <= 0:
            raise ValueError("new_quantity must be positive")
        if self.new_limit_price is not None and self.new_limit_price <= 0:
            raise ValueError("new_limit_price must be positive")


@dataclass(frozen=True, slots=True)
class BrokerCommandResult:
    outcome: CommandOutcome
    client_order_id: str
    observed_at: datetime
    broker_order_id: str | None = None
    error: BrokerError | None = None

    def __post_init__(self) -> None:
        _present(self.client_order_id, "client_order_id")
        _aware(self.observed_at, "observed_at")
        if self.outcome is CommandOutcome.ACCEPTED and self.error is not None:
            raise ValueError("accepted broker result cannot contain an error")
        if self.outcome is not CommandOutcome.ACCEPTED and self.error is None:
            raise ValueError("non-accepted broker result requires a typed error")
        if self.outcome is CommandOutcome.UNKNOWN and (
            self.error is None
            or self.error.retry is not RetryDirective.RECONCILE_THEN_DECIDE
        ):
            raise ValueError("unknown outcome must require reconciliation before retry")


@dataclass(frozen=True, slots=True)
class BrokerOrderEvent:
    schema_version: int
    event_id: str
    correlation_id: str
    causation_id: str
    sequence: int
    business_identity: str
    client_order_id: str
    broker_order_id: str | None
    state: BrokerOrderState
    reason_code: str
    event_time: datetime
    received_time: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.event_id, "event_id"),
            (self.correlation_id, "correlation_id"),
            (self.causation_id, "causation_id"),
            (self.business_identity, "business_identity"),
            (self.client_order_id, "client_order_id"),
            (self.reason_code, "reason_code"),
        ):
            _present(value, name)
        _aware(self.event_time, "event_time")
        _aware(self.received_time, "received_time")
        if self.schema_version <= 0 or self.sequence <= 0:
            raise ValueError("event schema_version and sequence must be positive")
        if self.received_time < self.event_time:
            raise ValueError("broker event cannot be received before event_time")


@dataclass(frozen=True, slots=True)
class BrokerFillEvent:
    schema_version: int
    event_id: str
    correlation_id: str
    causation_id: str
    sequence: int
    business_identity: str
    fill_id: str
    client_order_id: str
    broker_order_id: str
    instrument_id: str
    side: Side
    quantity: int
    price: Decimal
    event_time: datetime
    received_time: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.event_id, "event_id"),
            (self.correlation_id, "correlation_id"),
            (self.causation_id, "causation_id"),
            (self.business_identity, "business_identity"),
            (self.fill_id, "fill_id"),
            (self.client_order_id, "client_order_id"),
            (self.broker_order_id, "broker_order_id"),
            (self.instrument_id, "instrument_id"),
        ):
            _present(value, name)
        _aware(self.event_time, "event_time")
        _aware(self.received_time, "received_time")
        if self.schema_version <= 0 or self.sequence <= 0:
            raise ValueError("fill schema_version and sequence must be positive")
        if self.quantity <= 0 or self.price <= 0:
            raise ValueError("fill quantity and price must be positive")
        if self.received_time < self.event_time:
            raise ValueError("fill cannot be received before event_time")


BrokerEvent = BrokerOrderEvent | BrokerFillEvent


@dataclass(frozen=True, slots=True)
class BrokerOrderSnapshot:
    client_order_id: str
    broker_order_id: str | None
    state: BrokerOrderState
    quantity: int
    filled_quantity: int
    last_sequence: int

    def __post_init__(self) -> None:
        _present(self.client_order_id, "client_order_id")
        if self.quantity <= 0 or not 0 <= self.filled_quantity <= self.quantity:
            raise ValueError("broker order snapshot quantities are invalid")
        if self.last_sequence < 0:
            raise ValueError("last_sequence cannot be negative")


@dataclass(frozen=True, slots=True)
class ReconciliationSnapshot:
    as_of: datetime
    session_id: str
    orders: tuple[BrokerOrderSnapshot, ...]
    positions: tuple[tuple[str, int], ...]
    settled_cash: Decimal
    pending_settlements: tuple[tuple[str, Decimal], ...]
    fill_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _aware(self.as_of, "as_of")
        _present(self.session_id, "session_id")
        if len({item.client_order_id for item in self.orders}) != len(self.orders):
            raise ValueError("reconciliation orders must be unique by client_order_id")
        if len(set(self.fill_ids)) != len(self.fill_ids):
            raise ValueError("reconciliation fill identities must be unique")


@dataclass(frozen=True, slots=True)
class BrokerSessionHealth:
    state: SessionHealthState
    observed_at: datetime
    session_id: str | None
    kill_new_risk: bool
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _aware(self.observed_at, "observed_at")
        if self.state is not SessionHealthState.HEALTHY and not self.kill_new_risk:
            raise ValueError("non-healthy broker session must kill new risk")


@dataclass(frozen=True, slots=True)
class ReconciliationComparison:
    matched: bool
    kill_new_risk: bool
    mismatches: tuple[str, ...]


def compare_reconciliation(
    expected: ReconciliationSnapshot, actual: ReconciliationSnapshot
) -> ReconciliationComparison:
    """Compare authoritative identities and balances without guessing a repair."""
    mismatches: list[str] = []
    if expected.orders != actual.orders:
        mismatches.append("orders")
    if dict(expected.positions) != dict(actual.positions):
        mismatches.append("positions")
    if expected.settled_cash != actual.settled_cash:
        mismatches.append("settled_cash")
    if expected.pending_settlements != actual.pending_settlements:
        mismatches.append("pending_settlements")
    if set(expected.fill_ids) != set(actual.fill_ids):
        mismatches.append("fills")
    return ReconciliationComparison(not mismatches, bool(mismatches), tuple(mismatches))


@runtime_checkable
class BrokerSession(Protocol):
    @property
    def capabilities(self) -> BrokerCapabilities: ...

    def open(self, credential: CredentialReference) -> BrokerSessionHealth: ...

    def close(self) -> BrokerSessionHealth: ...

    def health(self) -> BrokerSessionHealth: ...

    def submit(self, request: SubmitOrderRequest) -> BrokerCommandResult: ...

    def cancel(self, request: CancelOrderRequest) -> BrokerCommandResult: ...

    def replace(self, request: ReplaceOrderRequest) -> BrokerCommandResult: ...

    def query_order(self, client_order_id: str) -> BrokerOrderSnapshot | None: ...

    def reconcile(self) -> ReconciliationSnapshot: ...
