"""Conservative deterministic target conversion and open-price fill simulation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from uuid import NAMESPACE_URL, uuid5

from island_quant.backtest.policies import FeeTaxPolicy, FillPolicy, TickSizePolicy
from island_quant.domain.models import (
    AssetType,
    Fill,
    Instrument,
    OrderIntent,
    OrderType,
    Side,
    TargetPosition,
)

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class ExecutionQuote:
    instrument: Instrument
    event_time: datetime
    open_price: Decimal
    close_price: Decimal
    volume: int
    suspended: bool = False
    limit_locked: bool = False

    def __post_init__(self) -> None:
        if self.event_time.tzinfo is None or self.event_time.utcoffset() is None:
            raise ValueError("execution quote time must be timezone-aware")
        if self.open_price <= ZERO or self.close_price <= ZERO or self.volume < 0:
            raise ValueError("execution quote values are invalid")


@dataclass(frozen=True, slots=True)
class FillOutcome:
    fill: Fill | None
    unfilled_quantity: int
    reason: str | None


class TargetOrderConverter:
    version = "target-weight-integer-shares-v1"

    def __init__(self, fee_policy: FeeTaxPolicy | None = None) -> None:
        self.fee_policy = fee_policy

    def convert(
        self,
        target: TargetPosition,
        *,
        portfolio_id: str,
        net_asset_value: Decimal,
        reference_price: Decimal,
        current_quantity: int,
        run_id: str,
    ) -> OrderIntent | None:
        if target.target_weight < ZERO:
            raise ValueError("Slice 1 target conversion forbids short positions")
        if net_asset_value <= ZERO or reference_price <= ZERO or current_quantity < 0:
            raise ValueError("target conversion inputs are invalid")
        if target.instrument.asset_type is not AssetType.EQUITY:
            raise ValueError("Slice 1 target conversion accepts ordinary equities only")
        target_quantity = int(
            (net_asset_value * target.target_weight / reference_price).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        if self.fee_policy is not None and target_quantity > current_quantity:
            budget = net_asset_value * target.target_weight
            while target_quantity > current_quantity:
                incremental = target_quantity - current_quantity
                gross = reference_price * incremental
                fee, _ = self.fee_policy.costs(Side.BUY, gross)
                if reference_price * target_quantity + fee <= budget:
                    break
                target_quantity -= 1
        delta = target_quantity - current_quantity
        if delta == 0:
            return None
        side = Side.BUY if delta > 0 else Side.SELL
        quantity = abs(delta)
        identity = (
            f"{run_id}:{target.strategy_id}:{target.instrument.key}:"
            f"{target.generated_at.isoformat()}:{side.value}:{quantity}"
        )
        return OrderIntent(
            portfolio_id=portfolio_id,
            strategy_id=target.strategy_id,
            instrument=target.instrument,
            side=side,
            quantity=quantity,
            order_type=OrderType.MARKET,
            created_at=target.generated_at,
            idempotency_key=hashlib.sha256(identity.encode()).hexdigest(),
            intent_id=uuid5(NAMESPACE_URL, identity),
        )


class ConservativeFillSimulator:
    def __init__(
        self,
        fill_policy: FillPolicy,
        fee_policy: FeeTaxPolicy,
        tick_policy: TickSizePolicy | None = None,
    ) -> None:
        self.fill_policy = fill_policy
        self.fee_policy = fee_policy
        self.tick_policy = tick_policy or TickSizePolicy()

    def simulate(
        self,
        intent: OrderIntent,
        quote: ExecutionQuote,
        *,
        run_id: str,
        cumulative_gross_before: Decimal = ZERO,
        commission_charged_before: Decimal = ZERO,
    ) -> FillOutcome:
        if intent.instrument.key != quote.instrument.key:
            raise ValueError("order and quote instruments differ")
        if quote.event_time <= intent.created_at:
            raise ValueError("fill must occur after target/order creation")
        if quote.suspended:
            return FillOutcome(None, intent.quantity, "suspended")
        if quote.limit_locked:
            return FillOutcome(None, intent.quantity, "price_limit_locked")
        if quote.volume <= 0:
            return FillOutcome(None, intent.quantity, "zero_volume")
        capacity = int(
            (
                Decimal(quote.volume) * self.fill_policy.maximum_volume_participation
            ).to_integral_value(rounding=ROUND_FLOOR)
        )
        filled_quantity = min(intent.quantity, capacity)
        if filled_quantity <= 0:
            return FillOutcome(None, intent.quantity, "participation_capacity_zero")
        if filled_quantity < intent.quantity and not self.fill_policy.allow_partial_fills:
            return FillOutcome(None, intent.quantity, "insufficient_volume_for_full_fill")
        price = self.estimated_price(intent, quote)
        gross = price * filled_quantity
        fee, tax = self.fee_policy.incremental_costs(
            intent.side, cumulative_gross_before, gross, commission_charged_before
        )
        identity = (
            f"{run_id}:{intent.intent_id}:{quote.event_time.isoformat()}:"
            f"{cumulative_gross_before}:{filled_quantity}"
        )
        fill = Fill(
            fill_id=hashlib.sha256(identity.encode()).hexdigest(),
            client_order_id=intent.idempotency_key,
            instrument=intent.instrument,
            side=intent.side,
            quantity=filled_quantity,
            price=price,
            fee=fee,
            tax=tax,
            event_time=quote.event_time,
            ingestion_time=quote.event_time,
        )
        reason = "partial_fill" if filled_quantity < intent.quantity else None
        return FillOutcome(fill, intent.quantity - filled_quantity, reason)

    def estimated_price(self, intent: OrderIntent, quote: ExecutionQuote) -> Decimal:
        """Return the same adverse, tick-rounded price used by deterministic simulation."""
        slip = self.fill_policy.slippage_bps / Decimal("10000")
        raw_price = quote.open_price * (
            Decimal("1") + slip if intent.side is Side.BUY else Decimal("1") - slip
        )
        rounding = ROUND_CEILING if intent.side is Side.BUY else ROUND_FLOOR
        candidate = raw_price
        for _ in range(3):
            tick = self.tick_policy.tick(candidate)
            rounded = (raw_price / tick).to_integral_value(rounding=rounding) * tick
            if self.tick_policy.tick(rounded) == tick:
                if rounded <= ZERO:
                    raise ValueError("rounded execution price must be positive")
                if intent.side is Side.BUY and rounded < quote.open_price:
                    raise ValueError("buy execution cannot improve on reference")
                if intent.side is Side.SELL and rounded > quote.open_price:
                    raise ValueError("sell execution cannot improve on reference")
                return rounded
            candidate = rounded
        raise ValueError("tick schedule did not converge")
