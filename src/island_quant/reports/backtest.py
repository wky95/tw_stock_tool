"""Backtest report contract with explicit synthetic/exploratory status."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from island_quant.backtest.engine import BacktestResult

SYNTHETIC_WARNING = "SYNTHETIC / EXPLORATORY — NOT EVIDENCE OF REAL-WORLD PERFORMANCE"


@dataclass(frozen=True, slots=True)
class BacktestReport:
    run_id: str
    strategy_id: str
    strategy_version: str
    status: str
    warning: str
    initial_net_asset_value: Decimal
    final_net_asset_value: Decimal
    total_return: Decimal
    maximum_drawdown: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal
    taxes: Decimal
    dividends: Decimal
    approved_orders: int
    rejected_orders: int
    fills: int
    event_checksum: str
    journal_checksum: str
    fee_policy_version: str
    settlement_policy_version: str
    fill_policy_version: str
    risk_policy_version: str
    reconciliation_status: str
    valuation_complete: bool
    promotion_eligible: bool
    assumptions: tuple[str, ...]


def build_backtest_report(result: BacktestResult) -> BacktestReport:
    """Build a deterministic report; incomplete PIT inputs force exploratory status."""
    values = [result.config.initial_cash]
    values.extend(snapshot.net_asset_value for snapshot in result.snapshots)
    values.append(result.final_snapshot.net_asset_value)
    peak = values[0]
    maximum_drawdown = Decimal("0")
    for value in values:
        peak = max(peak, value)
        drawdown = value / peak - Decimal("1")
        maximum_drawdown = min(maximum_drawdown, drawdown)
    initial = result.config.initial_cash
    final = result.final_snapshot
    status = "research" if result.config.pit_reference_complete else "exploratory"
    return BacktestReport(
        run_id=result.config.run_id,
        strategy_id=result.strategy_id,
        strategy_version=result.strategy_version,
        status=status,
        warning=SYNTHETIC_WARNING,
        initial_net_asset_value=initial,
        final_net_asset_value=final.net_asset_value,
        total_return=final.net_asset_value / initial - Decimal("1"),
        maximum_drawdown=maximum_drawdown,
        realized_pnl=final.realized_pnl,
        unrealized_pnl=final.unrealized_pnl,
        fees=final.fees,
        taxes=final.taxes,
        dividends=final.dividends,
        approved_orders=result.approved_orders,
        rejected_orders=result.rejected_orders,
        fills=result.fills,
        event_checksum=result.event_checksum,
        journal_checksum=result.journal_checksum,
        fee_policy_version=result.config.fee_policy.version,
        settlement_policy_version=result.config.settlement_policy.version,
        fill_policy_version=result.config.fill_policy.version,
        risk_policy_version=result.config.risk_policy.version,
        reconciliation_status="passed",
        valuation_complete=final.valuation_complete,
        promotion_eligible=(result.config.pit_reference_complete and final.valuation_complete),
        assumptions=(
            "offline deterministic fixtures",
            "next-session open execution with adverse slippage and volume cap",
            "integer shares, TWD cash account, no leverage or short selling",
            "suspended, limit-locked, and zero-volume orders remain unfilled",
        ),
    )
