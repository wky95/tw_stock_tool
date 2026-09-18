"""Idempotent pending-order reservations for cash and projected exposure."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from island_quant.backtest.policies import FeeTaxPolicy
from island_quant.domain.models import Fill, OrderIntent, Side

ZERO = Decimal("0")


class ReservationStatus(StrEnum):
    ACTIVE = "active"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class OrderReservation:
    intent_id: str
    idempotency_key: str
    instrument_key: str
    side: Side
    original_quantity: int
    remaining_quantity: int
    estimated_price: Decimal
    reserved_cash: Decimal
    cumulative_fill_gross: Decimal = ZERO
    commission_charged: Decimal = ZERO
    status: ReservationStatus = ReservationStatus.ACTIVE


class ReservationError(RuntimeError):
    pass


class PendingOrderBook:
    def __init__(self, fee_policy: FeeTaxPolicy) -> None:
        self.fee_policy = fee_policy
        self.reservations: dict[str, OrderReservation] = {}
        self._idempotency_index: dict[str, str] = {}

    @property
    def reserved_cash(self) -> Decimal:
        return sum(
            (
                item.reserved_cash
                for item in self.reservations.values()
                if item.status is ReservationStatus.ACTIVE
            ),
            ZERO,
        )

    def reserve(self, intent: OrderIntent, estimated_price: Decimal) -> OrderReservation:
        identity = str(intent.intent_id)
        existing_id = self._idempotency_index.get(intent.idempotency_key)
        if existing_id is not None:
            existing = self.reservations[existing_id]
            if existing.intent_id != identity:
                raise ReservationError("idempotency key belongs to another order intent")
            return existing
        gross = estimated_price * intent.quantity
        fee, tax = self.fee_policy.costs(intent.side, gross)
        reserved = gross + fee + tax if intent.side is Side.BUY else ZERO
        item = OrderReservation(
            identity,
            intent.idempotency_key,
            intent.instrument.key,
            intent.side,
            intent.quantity,
            intent.quantity,
            estimated_price,
            reserved,
        )
        self.reservations[identity] = item
        self._idempotency_index[intent.idempotency_key] = identity
        return item

    def apply_fill(self, intent_id: str, fill: Fill) -> OrderReservation:
        item = self._active(intent_id)
        if fill.client_order_id != item.idempotency_key:
            raise ReservationError("fill does not reference reserved order identity")
        if fill.instrument.key != item.instrument_key or fill.side is not item.side:
            raise ReservationError("fill instrument or side differs from reservation")
        if fill.quantity > item.remaining_quantity:
            raise ReservationError("fill exceeds remaining reserved quantity")
        remaining = item.remaining_quantity - fill.quantity
        cumulative_gross = item.cumulative_fill_gross + fill.price * fill.quantity
        commission = item.commission_charged + fill.fee
        if remaining:
            remaining_gross = item.estimated_price * remaining
            estimated_total_fee, estimated_tax = self.fee_policy.costs(
                item.side, cumulative_gross + remaining_gross
            )
            remaining_fee = max(ZERO, estimated_total_fee - commission)
            remaining_tax = estimated_tax if item.side is Side.BUY else ZERO
            reserved_cash = remaining_gross + remaining_fee + remaining_tax
            status = ReservationStatus.ACTIVE
        else:
            reserved_cash = ZERO
            status = ReservationStatus.FILLED
        updated = replace(
            item,
            remaining_quantity=remaining,
            reserved_cash=reserved_cash,
            cumulative_fill_gross=cumulative_gross,
            commission_charged=commission,
            status=status,
        )
        self.reservations[intent_id] = updated
        return updated

    def release(self, intent_id: str, status: ReservationStatus) -> OrderReservation:
        if status not in {
            ReservationStatus.CANCELLED,
            ReservationStatus.REJECTED,
            ReservationStatus.EXPIRED,
        }:
            raise ReservationError("release requires a terminal non-fill status")
        item = self._active(intent_id)
        updated = replace(item, reserved_cash=ZERO, status=status)
        self.reservations[intent_id] = updated
        return updated

    def projected_delta(self, instrument_key: str) -> int:
        return sum(
            (
                item.remaining_quantity if item.side is Side.BUY else -item.remaining_quantity
                for item in self.reservations.values()
                if item.status is ReservationStatus.ACTIVE and item.instrument_key == instrument_key
            ),
            0,
        )

    def reserved_sell_quantity(self, instrument_key: str) -> int:
        return sum(
            (
                item.remaining_quantity
                for item in self.reservations.values()
                if item.status is ReservationStatus.ACTIVE
                and item.instrument_key == instrument_key
                and item.side is Side.SELL
            ),
            0,
        )

    def _active(self, intent_id: str) -> OrderReservation:
        try:
            item = self.reservations[intent_id]
        except KeyError as exc:
            raise ReservationError("order intent has no reservation") from exc
        if item.status is not ReservationStatus.ACTIVE:
            raise ReservationError("order reservation is not active")
        return item
