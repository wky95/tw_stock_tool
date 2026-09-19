"""Offline deterministic paper broker; no network or order-book queue claim."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal


@dataclass(frozen=True, slots=True)
class PaperMarketEvent:
    instrument_id: str
    event_time: datetime
    reference_price: Decimal
    volume: int
    suspended: bool = False
    limit_locked: bool = False


@dataclass(frozen=True, slots=True)
class PaperOrderRequest:
    client_order_id: str
    idempotency_key: str
    instrument_id: str
    side: str
    quantity: int


@dataclass(frozen=True, slots=True)
class PaperFill:
    fill_id: str
    business_identity: str
    quantity: int
    price: Decimal
    event_time: datetime


@dataclass(frozen=True, slots=True)
class PaperBrokerResult:
    broker_order_id: str
    acknowledged_at: datetime
    status: str
    reason_code: str
    fills: tuple[PaperFill, ...]


@dataclass(frozen=True, slots=True)
class PaperBrokerPolicy:
    version: str = "deterministic-paper-broker-v1"
    latency_milliseconds: int = 50
    participation_cap: Decimal = Decimal("0.10")
    adverse_slippage_bps: Decimal = Decimal("5")
    reject_all: bool = False
    seed: int = 42


class DeterministicPaperBroker:
    def __init__(self, market: tuple[PaperMarketEvent, ...], policy: PaperBrokerPolicy) -> None:
        self.market = {item.instrument_id: item for item in market}
        self.policy = policy
        self._responses: dict[str, PaperBrokerResult] = {}
        self.requests: dict[str, PaperOrderRequest] = {}

    def submit(self, request: PaperOrderRequest) -> PaperBrokerResult:
        existing = self._responses.get(request.idempotency_key)
        if existing is not None:
            return existing
        event = self.market.get(request.instrument_id)
        if event is None:
            result = self._result(
                request,
                datetime(1970, 1, 1, tzinfo=UTC),
                "rejected",
                "missing_market_event",
                (),
            )
        elif self.policy.reject_all:
            result = self._result(request, event.event_time, "rejected", "injected_reject", ())
        elif event.suspended:
            result = self._result(
                request, event.event_time, "acknowledged", "suspended_no_fill", ()
            )
        elif event.limit_locked:
            result = self._result(
                request, event.event_time, "acknowledged", "limit_locked_no_fill", ()
            )
        elif event.volume <= 0:
            result = self._result(
                request, event.event_time, "acknowledged", "zero_volume_no_fill", ()
            )
        else:
            capacity = int(
                (Decimal(event.volume) * self.policy.participation_cap).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )
            quantity = min(request.quantity, capacity)
            slip = self.policy.adverse_slippage_bps / Decimal("10000")
            price = event.reference_price * (1 + slip if request.side == "buy" else 1 - slip)
            identity = f"{request.idempotency_key}:{quantity}:{price}"
            fill = PaperFill(
                hashlib.sha256(identity.encode()).hexdigest(),
                identity,
                quantity,
                price,
                event.event_time,
            )
            reason = "filled" if quantity == request.quantity else "partial_fill"
            result = self._result(request, event.event_time, "acknowledged", reason, (fill,))
        self.requests[request.client_order_id] = request
        self._responses[request.idempotency_key] = result
        return result

    def cancel(self, client_order_id: str) -> str:
        return "cancelled" if client_order_id in self.requests else "unknown_order"

    def replace(self, client_order_id: str, quantity: int) -> str:
        if quantity < 1:
            raise ValueError("replacement quantity must be positive")
        return "replaced" if client_order_id in self.requests else "unknown_order"

    def _result(
        self,
        request: PaperOrderRequest,
        at: datetime,
        status: str,
        reason: str,
        fills: tuple[PaperFill, ...],
    ) -> PaperBrokerResult:
        broker_id = hashlib.sha256(f"paper:{request.client_order_id}".encode()).hexdigest()
        acknowledged = at + timedelta(milliseconds=self.policy.latency_milliseconds)
        return PaperBrokerResult(broker_id, acknowledged, status, reason, fills)
