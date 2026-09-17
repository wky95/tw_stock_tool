"""Immutable core domain models with finance-specific invariants."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


class Market(StrEnum):
    TWSE = "TWSE"
    TPEX = "TPEx"


class AssetType(StrEnum):
    EQUITY = "equity"
    ETF = "etf"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class TimeInForce(StrEnum):
    DAY = "day"
    IOC = "ioc"
    FOK = "fok"


class OrderStatus(StrEnum):
    CREATED = "created"
    RISK_APPROVED = "risk_approved"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class RiskOutcome(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    market: Market
    asset_type: AssetType = AssetType.EQUITY
    currency: str = "TWD"
    lot_size: int = 1000
    price_tick: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol must not be empty")
        if self.lot_size <= 0 or self.price_tick <= 0:
            raise ValueError("lot_size and price_tick must be positive")

    @property
    def key(self) -> str:
        return f"{self.market.value}:{self.symbol}"


@dataclass(frozen=True, slots=True)
class Bar:
    instrument: Instrument
    event_time: datetime
    available_time: datetime
    ingestion_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    source: str
    data_version: str

    def __post_init__(self) -> None:
        for name in ("event_time", "available_time", "ingestion_time"):
            _require_aware(getattr(self, name), name)
        if not self.event_time <= self.available_time <= self.ingestion_time:
            message = "timestamps must satisfy event_time <= available_time <= ingestion_time"
            raise ValueError(message)
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC prices must be positive")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("OHLC values are inconsistent")
        if self.volume < 0:
            raise ValueError("volume must not be negative")
        if not self.source or not self.data_version:
            raise ValueError("source and data_version are required for lineage")


@dataclass(frozen=True, slots=True)
class Feature:
    instrument: Instrument
    name: str
    version: str
    as_of: datetime
    available_time: datetime
    value: Decimal | None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_aware(self.as_of, "as_of")
        _require_aware(self.available_time, "available_time")
        if self.available_time < self.as_of:
            raise ValueError("feature cannot be available before its as_of time")
        if not self.name or not self.version:
            raise ValueError("feature name and version are required")


@dataclass(frozen=True, slots=True)
class Signal:
    strategy_id: str
    instrument: Instrument
    generated_at: datetime
    value: Decimal
    feature_set_version: str
    model_version: str | None = None

    def __post_init__(self) -> None:
        _require_aware(self.generated_at, "generated_at")


@dataclass(frozen=True, slots=True)
class Forecast:
    strategy_id: str
    instrument: Instrument
    generated_at: datetime
    horizon: str
    expected_return: Decimal
    model_version: str

    def __post_init__(self) -> None:
        _require_aware(self.generated_at, "generated_at")


@dataclass(frozen=True, slots=True)
class TargetPosition:
    strategy_id: str
    instrument: Instrument
    generated_at: datetime
    target_weight: Decimal
    reason: str

    def __post_init__(self) -> None:
        _require_aware(self.generated_at, "generated_at")
        if not Decimal("-1") <= self.target_weight <= Decimal("1"):
            raise ValueError("target_weight must be between -1 and 1")


@dataclass(frozen=True, slots=True)
class OrderIntent:
    portfolio_id: str
    strategy_id: str
    instrument: Instrument
    side: Side
    quantity: int
    order_type: OrderType
    created_at: datetime
    idempotency_key: str
    limit_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    intent_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "created_at")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price must be positive")
        if not self.idempotency_key:
            raise ValueError("idempotency_key is required")


@dataclass(frozen=True, slots=True)
class Order:
    intent_id: UUID
    client_order_id: str
    status: OrderStatus
    submitted_quantity: int
    filled_quantity: int = 0
    broker_order_id: str | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.submitted_quantity <= 0:
            raise ValueError("submitted_quantity must be positive")
        if not 0 <= self.filled_quantity <= self.submitted_quantity:
            raise ValueError("filled_quantity is outside submitted quantity")
        if self.updated_at is not None:
            _require_aware(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str
    client_order_id: str
    instrument: Instrument
    side: Side
    quantity: int
    price: Decimal
    fee: Decimal
    tax: Decimal
    event_time: datetime
    ingestion_time: datetime

    def __post_init__(self) -> None:
        _require_aware(self.event_time, "event_time")
        _require_aware(self.ingestion_time, "ingestion_time")
        if self.ingestion_time < self.event_time:
            raise ValueError("fill ingestion_time cannot precede event_time")
        if self.quantity <= 0 or self.price <= 0:
            raise ValueError("fill quantity and price must be positive")
        if self.fee < 0 or self.tax < 0:
            raise ValueError("fill costs cannot be negative")


@dataclass(frozen=True, slots=True)
class Position:
    instrument: Instrument
    quantity: int
    average_cost: Decimal
    market_price: Decimal
    as_of: datetime

    def __post_init__(self) -> None:
        _require_aware(self.as_of, "as_of")
        if self.average_cost < 0 or self.market_price < 0:
            raise ValueError("position prices cannot be negative")


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    portfolio_id: str
    as_of: datetime
    cash: Decimal
    net_asset_value: Decimal
    positions: tuple[Position, ...]
    sequence: int

    def __post_init__(self) -> None:
        _require_aware(self.as_of, "as_of")
        if self.net_asset_value < 0 or self.sequence < 0:
            raise ValueError("NAV and sequence cannot be negative")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    intent_id: UUID
    outcome: RiskOutcome
    rule_version: str
    evaluated_at: datetime
    reasons: tuple[str, ...]
    inputs: dict[str, Any]

    def __post_init__(self) -> None:
        _require_aware(self.evaluated_at, "evaluated_at")
        if not self.rule_version:
            raise ValueError("rule_version is required")
        if self.outcome is RiskOutcome.REJECT and not self.reasons:
            raise ValueError("rejected risk decisions require at least one reason")
