"""Minimal fail-closed pre-trade risk gate for long-only cash backtests."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from island_quant.backtest.policies import FeeTaxPolicy, RiskPolicy
from island_quant.domain.models import (
    AssetType,
    OrderIntent,
    RiskDecision,
    RiskOutcome,
    Side,
)
from island_quant.portfolio.accounting import PortfolioLedger
from island_quant.risk.reservations import PendingOrderBook

ZERO = Decimal("0")


class PreTradeRiskEngine:
    def __init__(self, policy: RiskPolicy, fee_policy: FeeTaxPolicy) -> None:
        self.policy = policy
        self.fee_policy = fee_policy

    def evaluate(
        self,
        intent: OrderIntent,
        *,
        estimated_price: Decimal,
        ledger: PortfolioLedger,
        net_asset_value: Decimal,
        evaluated_at: datetime,
        pending_orders: PendingOrderBook | None = None,
    ) -> RiskDecision:
        reasons: list[str] = []
        if estimated_price <= ZERO or net_asset_value <= ZERO:
            reasons.append("invalid_valuation_input")
        if intent.instrument.asset_type is not AssetType.EQUITY:
            reasons.append("unsupported_asset_type")
        if intent.instrument.currency != "TWD":
            reasons.append("unsupported_currency")
        current_quantity = ledger.quantity(intent.instrument)
        pending_delta = (
            pending_orders.projected_delta(intent.instrument.key)
            if pending_orders is not None
            else 0
        )
        projected_quantity = (
            current_quantity + pending_delta + intent.quantity
            if intent.side is Side.BUY
            else current_quantity + pending_delta - intent.quantity
        )
        reserved_sell = (
            pending_orders.reserved_sell_quantity(intent.instrument.key)
            if pending_orders is not None
            else 0
        )
        if intent.side is Side.SELL and intent.quantity > current_quantity - reserved_sell:
            reasons.append("short_position_forbidden")
        notional = estimated_price * intent.quantity
        if notional > self.policy.maximum_order_notional:
            reasons.append("maximum_order_notional_exceeded")
        if net_asset_value > ZERO:
            projected_weight = estimated_price * max(0, projected_quantity) / net_asset_value
            if projected_weight > self.policy.maximum_position_weight:
                reasons.append("maximum_position_weight_exceeded")
            other_market_value = sum(
                (
                    state.market_value
                    for key, state in ledger.positions.items()
                    if key != intent.instrument.key
                ),
                ZERO,
            )
            if pending_orders is not None:
                other_market_value += sum(
                    (
                        reservation.estimated_price
                        * (
                            reservation.remaining_quantity
                            if reservation.side is Side.BUY
                            else -reservation.remaining_quantity
                        )
                        for reservation in pending_orders.reservations.values()
                        if reservation.status.value == "active"
                        and reservation.instrument_key != intent.instrument.key
                    ),
                    ZERO,
                )
            projected_gross = other_market_value + estimated_price * max(0, projected_quantity)
            if projected_gross / net_asset_value > self.policy.maximum_gross_exposure:
                reasons.append("maximum_gross_exposure_exceeded")
        if notional > ZERO:
            estimated_fee, estimated_tax = self.fee_policy.costs(intent.side, notional)
        else:
            estimated_fee, estimated_tax = ZERO, ZERO
        estimated_cash_required = notional + estimated_fee + estimated_tax
        buying_power = ledger.available_cash - (
            pending_orders.reserved_cash if pending_orders is not None else ZERO
        )
        if intent.side is Side.BUY and estimated_cash_required > buying_power:
            reasons.append("insufficient_available_cash")
        outcome = RiskOutcome.REJECT if reasons else RiskOutcome.APPROVE
        return RiskDecision(
            intent_id=intent.intent_id,
            outcome=outcome,
            rule_version=self.policy.version,
            evaluated_at=evaluated_at,
            reasons=tuple(reasons),
            inputs={
                "estimated_price": str(estimated_price),
                "notional": str(notional),
                "estimated_fee": str(estimated_fee),
                "estimated_tax": str(estimated_tax),
                "available_cash": str(ledger.available_cash),
                "pending_reserved_cash": str(
                    pending_orders.reserved_cash if pending_orders is not None else ZERO
                ),
                "buying_power": str(buying_power),
                "current_quantity": current_quantity,
                "projected_quantity": projected_quantity,
                "net_asset_value": str(net_asset_value),
            },
        )
