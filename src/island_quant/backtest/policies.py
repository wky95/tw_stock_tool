"""Versioned Taiwan cash-equity backtest policies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from island_quant.domain.models import Side

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class FeeTaxPolicy:
    version: str = "tw-cash-equity-costs-engineering-fixture-v2"
    commission_rate: Decimal = Decimal("0.001425")
    commission_discount: Decimal = Decimal("1")
    minimum_commission: Decimal = Decimal("20")
    sell_transaction_tax_rate: Decimal = Decimal("0.003")
    currency: str = "TWD"
    minimum_commission_scope: str = "per_order_lifecycle"
    jurisdiction: str = "TW"
    asset_type: str = "cash_equity"
    purpose: str = "engineering_fixture"
    effective_from: date = date(2024, 1, 1)
    effective_to: date | None = None
    broker_specific: bool = False
    verified_source: str | None = None
    assumptions: tuple[str, ...] = (
        "minimum commission is charged once across all fills in an order lifecycle",
        "rules must be re-verified before broker integration",
    )

    def __post_init__(self) -> None:
        if not self.version or self.currency != "TWD":
            raise ValueError("fee policy requires a version and TWD currency")
        values = (
            self.commission_rate,
            self.commission_discount,
            self.minimum_commission,
            self.sell_transaction_tax_rate,
        )
        if any(value < ZERO for value in values) or self.commission_discount > Decimal("1"):
            raise ValueError("fee and tax policy values are invalid")
        if self.minimum_commission_scope != "per_order_lifecycle":
            raise ValueError("unsupported minimum commission scope")

    def costs(self, side: Side, gross_notional: Decimal) -> tuple[Decimal, Decimal]:
        if gross_notional <= ZERO:
            raise ValueError("gross notional must be positive")
        raw_commission = gross_notional * self.commission_rate * self.commission_discount
        fee = max(self.minimum_commission, raw_commission).quantize(
            Decimal("1"), rounding=ROUND_CEILING
        )
        tax = (
            (gross_notional * self.sell_transaction_tax_rate).quantize(
                Decimal("1"), rounding=ROUND_FLOOR
            )
            if side is Side.SELL
            else ZERO
        )
        return fee, tax

    def incremental_costs(
        self,
        side: Side,
        cumulative_gross_before: Decimal,
        fill_gross: Decimal,
        commission_charged_before: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Assess one order's incremental costs without repeating the minimum fee."""
        if cumulative_gross_before < ZERO or commission_charged_before < ZERO:
            raise ValueError("cumulative order costs cannot be negative")
        cumulative_fee, _ = self.costs(side, cumulative_gross_before + fill_gross)
        fee = cumulative_fee - commission_charged_before
        if fee < ZERO:
            raise ValueError("commission already charged exceeds cumulative assessment")
        tax = (
            (fill_gross * self.sell_transaction_tax_rate).quantize(
                Decimal("1"), rounding=ROUND_FLOOR
            )
            if side is Side.SELL
            else ZERO
        )
        return fee, tax


@dataclass(frozen=True, slots=True)
class SettlementPolicy:
    version: str = "tw-cash-equity-gross-t-plus-2-engineering-fixture-v2"
    lag_sessions: int = 2
    currency: str = "TWD"
    obligation_netting: str = "gross_per_obligation"
    jurisdiction: str = "TW"
    asset_type: str = "cash_equity"
    purpose: str = "engineering_fixture"
    effective_from: date = date(2024, 1, 1)
    effective_to: date | None = None
    broker_specific: bool = False
    verified_source: str | None = None
    assumptions: tuple[str, ...] = (
        "only dates present in the pinned open-session calendar count",
        "receivables and payables settle as separate gross obligations",
    )

    def __post_init__(self) -> None:
        if not self.version or self.lag_sessions < 0 or self.currency != "TWD":
            raise ValueError("settlement policy is invalid")
        if self.obligation_netting != "gross_per_obligation":
            raise ValueError("unsupported settlement netting policy")

    def due_date(self, trade_date: date, trading_sessions: tuple[date, ...]) -> date:
        try:
            index = trading_sessions.index(trade_date)
        except ValueError as exc:
            raise ValueError("trade date is absent from the pinned calendar") from exc
        due_index = index + self.lag_sessions
        if due_index >= len(trading_sessions):
            raise ValueError("pinned calendar lacks required settlement sessions")
        return trading_sessions[due_index]


@dataclass(frozen=True, slots=True)
class FillPolicy:
    version: str = "conservative-open-fill-v1"
    slippage_bps: Decimal = Decimal("10")
    maximum_volume_participation: Decimal = Decimal("0.10")
    allow_partial_fills: bool = True
    cancel_unfilled_after_session: bool = True
    jurisdiction: str = "TW"
    asset_type: str = "cash_equity"
    purpose: str = "engineering_fixture"
    effective_from: date = date(2024, 1, 1)
    effective_to: date | None = None
    broker_specific: bool = False
    verified_source: str | None = None
    assumptions: tuple[str, ...] = (
        "orders execute from a synthetic next-session open quote",
        "unfilled residual quantity is cancelled after the fixture session",
    )

    def __post_init__(self) -> None:
        if not self.version or self.slippage_bps < ZERO:
            raise ValueError("fill policy version and slippage must be valid")
        if not ZERO < self.maximum_volume_participation <= Decimal("1"):
            raise ValueError("volume participation must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    version: str = "long-only-cash-risk-v1"
    maximum_position_weight: Decimal = Decimal("0.40")
    maximum_order_notional: Decimal = Decimal("5000000")
    maximum_gross_exposure: Decimal = Decimal("1")
    allow_short: bool = False
    allow_leverage: bool = False
    jurisdiction: str = "TW"
    asset_type: str = "cash_equity"
    purpose: str = "engineering_fixture"
    effective_from: date = date(2024, 1, 1)
    effective_to: date | None = None
    broker_specific: bool = False
    verified_source: str | None = None
    assumptions: tuple[str, ...] = (
        "pending orders reserve cash and projected exposure",
        "unsettled sale proceeds do not provide buying power",
    )

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("risk policy version is required")
        if not ZERO < self.maximum_position_weight <= Decimal("1"):
            raise ValueError("maximum position weight must be in (0, 1]")
        if self.maximum_order_notional <= ZERO or self.maximum_gross_exposure != Decimal("1"):
            raise ValueError("Slice 1 risk policy requires positive notional and gross exposure 1")
        if self.allow_short or self.allow_leverage:
            raise ValueError("Slice 1 forbids short selling and leverage")


@dataclass(frozen=True, slots=True)
class MarkPolicy:
    version: str = "last-valid-close-mark-engineering-fixture-v1"
    maximum_stale_sessions: int = 2
    jurisdiction: str = "TW"
    asset_type: str = "cash_equity"
    purpose: str = "engineering_fixture"
    effective_from: date = date(2024, 1, 1)
    effective_to: date | None = None
    broker_specific: bool = False
    verified_source: str | None = None
    assumptions: tuple[str, ...] = (
        "last valid close may be carried only within the stale-session limit",
    )

    def __post_init__(self) -> None:
        if not self.version or self.maximum_stale_sessions < 0:
            raise ValueError("mark policy is invalid")


@dataclass(frozen=True, slots=True)
class TickSizePolicy:
    version: str = "tw-cash-equity-tick-schedule-engineering-fixture-v1"
    purpose: str = "engineering_fixture"
    jurisdiction: str = "TW"
    asset_type: str = "cash_equity"
    effective_from: date = date(2024, 1, 1)
    effective_to: date | None = None
    broker_specific: bool = False
    verified_source: str | None = None
    assumptions: tuple[str, ...] = (
        "price bands are an offline fixture and require exchange verification",
    )
    bands: tuple[tuple[Decimal | None, Decimal], ...] = (
        (Decimal("10"), Decimal("0.01")),
        (Decimal("50"), Decimal("0.05")),
        (Decimal("100"), Decimal("0.1")),
        (Decimal("500"), Decimal("0.5")),
        (Decimal("1000"), Decimal("1")),
        (None, Decimal("5")),
    )

    def tick(self, price: Decimal) -> Decimal:
        if price <= ZERO:
            raise ValueError("tick lookup price must be positive")
        for upper, tick in self.bands:
            if upper is None or price < upper:
                return tick
        raise AssertionError("tick schedule must have an unbounded final band")
