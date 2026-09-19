"""Deterministic performance, benchmark, and reconciled cost attribution."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from island_quant.backtest.engine import BacktestResult
from island_quant.backtest.events import BacktestEventKind

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class PerformancePolicy:
    version: str = "performance-analytics-v1"
    annualization_factor: int = 252
    risk_free_rate_annual: Decimal = ZERO
    minimum_acceptable_return_annual: Decimal = ZERO
    minimum_return_observations: int = 3
    return_frequency: str = "trading_session"
    risk_free_session_conversion: str = "annual_rate / annualization_factor"
    volatility_ddof: int = 1
    cagr_elapsed_time_convention: str = "actual_calendar_days_365.25"
    annualization_time_convention: str = "trading_sessions"
    turnover_definition: str = "executed_gross_notional / average_sampled_NAV"
    exposure_sampling_time: str = "session_close"
    cash_weight_sampling_time: str = "session_close_available_TWD_cash"

    def __post_init__(self) -> None:
        if self.annualization_factor < 1 or self.minimum_return_observations < 1:
            raise ValueError("performance observation policy is invalid")
        if self.volatility_ddof != 1:
            raise ValueError("Slice 2 supports sample volatility with ddof=1")


@dataclass(frozen=True, slots=True)
class DrawdownPoint:
    session: date
    drawdown: Decimal


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    initial_equity: Decimal
    ending_equity: Decimal
    total_return: Decimal
    cagr: float | None
    daily_mean_return: float | None
    annualized_volatility: float | None
    sharpe_ratio: float | None
    sortino_ratio: float | None
    maximum_drawdown: Decimal
    maximum_drawdown_start: date | None
    maximum_drawdown_trough: date | None
    maximum_drawdown_recovery: date | None
    drawdown_duration_sessions: int
    drawdown_ongoing: bool
    calmar_ratio: float | None
    positive_day_ratio: float | None
    turnover: Decimal
    strategy_driven_turnover: Decimal
    data_quality_driven_turnover: Decimal
    forced_liquidation_count: int
    forced_liquidation_costs: Decimal
    average_gross_exposure: Decimal
    average_net_exposure: Decimal
    average_cash_weight: Decimal
    number_of_orders: int
    number_of_fills: int
    partial_fills: int
    rejections: int
    unfilled_quantity: int
    fees: Decimal
    taxes: Decimal
    slippage_cost: Decimal
    dividend_income: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    reconciliation_residual: Decimal
    daily_returns: tuple[tuple[date, Decimal], ...]
    drawdown_series: tuple[DrawdownPoint, ...]
    policy: PerformancePolicy
    warning: str
    checksum: str


class PerformanceAnalyzer:
    def analyze(
        self,
        result: BacktestResult,
        policy: PerformancePolicy,
        *,
        external_cash_flows: tuple[tuple[date, Decimal], ...] = (),
    ) -> PerformanceReport:
        if external_cash_flows:
            raise ValueError("external cash flows are unsupported in Slice 2")
        initial = result.config.initial_cash
        ending = result.final_snapshot.net_asset_value
        if initial <= ZERO or ending <= ZERO:
            raise ValueError("performance metrics require positive initial and ending NAV")
        navs = [(snapshot.as_of.date(), snapshot.net_asset_value) for snapshot in result.snapshots]
        if any(nav <= ZERO for _, nav in navs):
            raise ValueError("performance metrics require positive NAV at every session")
        returns: list[tuple[date, Decimal]] = []
        previous = initial
        for session, nav in navs:
            if previous <= ZERO:
                raise ValueError("session return denominator must be positive")
            returns.append((session, nav / previous - Decimal("1")))
            previous = nav
        values = [initial, *(nav for _, nav in navs)]
        first_session = navs[0][0] if navs else result.final_snapshot.as_of.date()
        sessions = [first_session, *(day for day, _ in navs)]
        drawdowns, start, trough, recovery, duration, ongoing = _drawdowns(sessions, values)
        numeric = [float(value) for _, value in returns]
        enough = len(numeric) >= policy.minimum_return_observations
        mean = statistics.fmean(numeric) if enough else None
        volatility = _standard_deviation(numeric, policy.volatility_ddof) if enough else None
        annual_volatility = (
            volatility * math.sqrt(policy.annualization_factor) if volatility is not None else None
        )
        per_session_rf = float(policy.risk_free_rate_annual) / policy.annualization_factor
        sharpe = (
            (mean - per_session_rf) / volatility * math.sqrt(policy.annualization_factor)
            if mean is not None and volatility is not None and volatility != 0.0
            else None
        )
        per_session_mar = (
            float(policy.minimum_acceptable_return_annual) / policy.annualization_factor
        )
        downside = [min(0.0, value - per_session_mar) for value in numeric]
        downside_deviation = (
            math.sqrt(statistics.fmean(value * value for value in downside)) if enough else None
        )
        sortino = (
            (mean - per_session_mar)
            / downside_deviation
            * math.sqrt(policy.annualization_factor)
            if (
                mean is not None
                and downside_deviation is not None
                and downside_deviation != 0.0
            )
            else None
        )
        elapsed_days = (result.final_snapshot.as_of.date() - sessions[0]).days
        cagr = (
            float((ending / initial) ** (Decimal("365.25") / elapsed_days) - Decimal("1"))
            if elapsed_days > 0
            else None
        )
        maximum_drawdown = min((item.drawdown for item in drawdowns), default=ZERO)
        calmar = (
            cagr / abs(float(maximum_drawdown))
            if cagr is not None and maximum_drawdown
            else None
        )
        fills = [
            event
            for event in result.events
            if event.kind is BacktestEventKind.FILL_RECEIVED
            and event.payload["status"] in {"filled", "partial"}
        ]
        traded = sum(
            (
                Decimal(str(event.payload["fill"]["price"]))
                * int(event.payload["fill"]["quantity"])
                for event in fills
            ),
            ZERO,
        )
        data_quality_fills = [
            event
            for event in fills
            if str(event.payload.get("target_reason", "")).startswith("data_quality:")
        ]
        data_quality_notional = sum(
            (
                Decimal(str(event.payload["fill"]["price"]))
                * int(event.payload["fill"]["quantity"])
                for event in data_quality_fills
            ),
            ZERO,
        )
        forced_costs = sum(
            (
                Decimal(str(event.payload["fill"]["fee"]))
                + Decimal(str(event.payload["fill"]["tax"]))
                + _event_slippage(event)
                for event in data_quality_fills
            ),
            ZERO,
        )
        average_nav = sum(values, ZERO) / len(values)
        exposures = [
            snapshot.positions_market_value / snapshot.net_asset_value
            for snapshot in result.snapshots
            if snapshot.net_asset_value > ZERO
        ]
        cash_weights = [
            snapshot.available_cash / snapshot.net_asset_value
            for snapshot in result.snapshots
            if snapshot.net_asset_value > ZERO
        ]
        slippage = _slippage_cost(fills)
        final = result.final_snapshot
        payload: dict[str, Any] = {
            "initial_equity": initial,
            "ending_equity": ending,
            "daily_returns": returns,
            "policy": asdict(policy),
            "event_checksum": result.event_checksum,
        }
        checksum = hashlib.sha256(_canonical(payload)).hexdigest()
        return PerformanceReport(
            initial,
            ending,
            ending / initial - Decimal("1"),
            cagr,
            mean,
            annual_volatility,
            sharpe,
            sortino,
            maximum_drawdown,
            start,
            trough,
            recovery,
            duration,
            ongoing,
            calmar,
            sum(value > 0 for value in numeric) / len(numeric) if enough else None,
            traded / average_nav,
            (traded - data_quality_notional) / average_nav,
            data_quality_notional / average_nav,
            len(data_quality_fills),
            forced_costs,
            sum(exposures, ZERO) / len(exposures) if exposures else ZERO,
            sum(exposures, ZERO) / len(exposures) if exposures else ZERO,
            sum(cash_weights, ZERO) / len(cash_weights) if cash_weights else Decimal("1"),
            sum(event.kind is BacktestEventKind.ORDER_INTENT_CREATED for event in result.events),
            len(fills),
            sum(event.payload["status"] == "partial" for event in fills),
            result.rejected_orders,
            sum(
                int(event.payload.get("unfilled_quantity", 0))
                for event in result.events
                if event.kind is BacktestEventKind.FILL_RECEIVED
            ),
            final.fees,
            final.taxes,
            slippage,
            final.dividends,
            final.realized_pnl,
            final.unrealized_pnl,
            final.reconciliation_residual,
            tuple(returns),
            tuple(drawdowns),
            policy,
            "SYNTHETIC / EXPLORATORY — NOT STATISTICAL OR INVESTMENT EVIDENCE",
            checksum,
        )


@dataclass(frozen=True, slots=True)
class BenchmarkSeries:
    benchmark_id: str
    benchmark_kind: str
    dataset_version: str
    initial_level: Decimal
    sessions: tuple[tuple[date, Decimal], ...]
    pit_market_cap_complete: bool = True

    def __post_init__(self) -> None:
        if self.dataset_version in {"", "latest", "current"}:
            raise ValueError("benchmark requires an exact dataset version")
        if (
            self.benchmark_kind == "eligible_universe_market_cap"
            and not self.pit_market_cap_complete
        ):
            raise ValueError("market-cap benchmark requires complete PIT market cap")
        if self.initial_level <= ZERO or any(value <= ZERO for _, value in self.sessions):
            raise ValueError("benchmark levels must be positive")


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    benchmark_id: str
    benchmark_version: str
    total_return: Decimal
    active_return: Decimal
    tracking_error: float | None
    information_ratio: float | None
    beta: float | None
    alpha_annual: float | None
    daily: tuple[tuple[date, Decimal, Decimal, Decimal], ...]


def analyze_benchmark(
    strategy_returns: tuple[tuple[date, Decimal], ...],
    benchmark: BenchmarkSeries,
    annualization_factor: int,
) -> BenchmarkReport:
    levels = dict(benchmark.sessions)
    expected = tuple(day for day, _ in strategy_returns)
    if tuple(day for day, _ in benchmark.sessions) != expected:
        raise ValueError("benchmark sessions must align exactly; future backfill is forbidden")
    previous = benchmark.initial_level
    rows: list[tuple[date, Decimal, Decimal, Decimal]] = []
    for day, strategy_return in strategy_returns:
        level = levels[day]
        benchmark_return = level / previous - Decimal("1")
        rows.append((day, strategy_return, benchmark_return, strategy_return - benchmark_return))
        previous = level
    benchmark_total = benchmark.sessions[-1][1] / benchmark.initial_level - Decimal("1")
    strategy_growth = math.prod((Decimal("1") + row[1] for row in rows), start=Decimal("1"))
    active = strategy_growth - Decimal("1") - benchmark_total
    active_values = [float(row[3]) for row in rows]
    strategy_values = [float(row[1]) for row in rows]
    benchmark_values = [float(row[2]) for row in rows]
    tracking = (
        statistics.stdev(active_values) * math.sqrt(annualization_factor)
        if len(active_values) >= 3
        else None
    )
    information = (
        statistics.fmean(active_values) / statistics.stdev(active_values)
        * math.sqrt(annualization_factor)
        if len(active_values) >= 3 and statistics.stdev(active_values) != 0
        else None
    )
    beta: float | None = None
    alpha: float | None = None
    if len(rows) >= 3 and statistics.variance(benchmark_values) != 0:
        covariance = statistics.covariance(strategy_values, benchmark_values)
        beta = covariance / statistics.variance(benchmark_values)
        alpha = (
            statistics.fmean(strategy_values) - beta * statistics.fmean(benchmark_values)
        ) * annualization_factor
    return BenchmarkReport(
        benchmark.benchmark_id,
        benchmark.dataset_version,
        benchmark_total,
        active,
        tracking,
        information,
        beta,
        alpha,
        tuple(rows),
    )


@dataclass(frozen=True, slots=True)
class AttributionRow:
    key: str
    gross_price_movement: Decimal
    dividend_pnl: Decimal
    brokerage_fees: Decimal
    transaction_taxes: Decimal
    slippage_cost: Decimal
    corporate_action_effects: Decimal
    external_flows: Decimal
    total: Decimal
    realized_pnl_memo: Decimal
    unrealized_pnl_memo: Decimal
    residual: Decimal


@dataclass(frozen=True, slots=True)
class AttributionReport:
    identity: str
    portfolio: AttributionRow
    per_session: tuple[AttributionRow, ...]
    notional_allocated_attribution: tuple[AttributionRow, ...]
    allocation_warning: str
    checksum: str


def attribute(result: BacktestResult, performance: PerformanceReport) -> AttributionReport:
    action_types = {
        str(event.payload.get("action_type"))
        for event in result.events
        if event.kind is BacktestEventKind.CORPORATE_ACTION_APPLIED
    }
    unsupported_actions = action_types - {"cash_dividend"}
    if unsupported_actions:
        raise ValueError(
            "corporate-action economic attribution is unsupported and cannot be classified"
        )
    delta = performance.ending_equity - performance.initial_equity
    price = _cumulative_price_movement(result, result.final_snapshot.positions_market_value)
    portfolio = _attribution_row(
        "portfolio",
        price,
        performance.dividend_income,
        performance.fees,
        performance.taxes,
        performance.slippage_cost,
        performance.realized_pnl,
        performance.unrealized_pnl,
        delta,
    )
    sessions: list[AttributionRow] = []
    prior_nav = performance.initial_equity
    prior_fees = prior_taxes = prior_dividends = ZERO
    prior_price = ZERO
    for snapshot in result.snapshots:
        change = snapshot.net_asset_value - prior_nav
        fee = snapshot.fees - prior_fees
        tax = snapshot.taxes - prior_taxes
        dividend = snapshot.dividends - prior_dividends
        slip = sum(
            (
                _event_slippage(event)
                for event in result.events
                if event.kind is BacktestEventKind.FILL_RECEIVED
                and event.occurred_at.date() == snapshot.as_of.date()
            ),
            ZERO,
        )
        cumulative_price = _cumulative_price_movement(
            result, snapshot.positions_market_value, through=snapshot.as_of.date()
        )
        gross = cumulative_price - prior_price
        sessions.append(
            _attribution_row(
                snapshot.as_of.date().isoformat(),
                gross,
                dividend,
                fee,
                tax,
                slip,
                ZERO,
                ZERO,
                change,
            )
        )
        prior_nav = snapshot.net_asset_value
        prior_fees, prior_taxes, prior_dividends = (
            snapshot.fees,
            snapshot.taxes,
            snapshot.dividends,
        )
        prior_price = cumulative_price
    instruments = _instrument_rows(result, portfolio)
    if sum((item.total for item in sessions), ZERO) != portfolio.total:
        raise RuntimeError("session attribution does not reconcile to portfolio attribution")
    if instruments and sum((item.total for item in instruments), ZERO) != portfolio.total:
        raise RuntimeError("notional allocation does not reconcile to portfolio attribution")
    checksum = hashlib.sha256(
        _canonical(
            {
                "portfolio": asdict(portfolio),
                "sessions": [asdict(item) for item in sessions],
                "notional_allocated_attribution": [asdict(item) for item in instruments],
            }
        )
    ).hexdigest()
    return AttributionReport(
        "ΔNAV = gross price + dividends - fees - taxes - slippage + corporate actions + flows",
        portfolio,
        tuple(sessions),
        instruments,
        (
            "Executed-notional allocation for reconciliation only; it is not a security-level "
            "economic contribution and must not support security-selection conclusions."
        ),
        checksum,
    )


def _attribution_row(
    key: str,
    price: Decimal,
    dividend: Decimal,
    fees: Decimal,
    taxes: Decimal,
    slippage: Decimal,
    realized: Decimal,
    unrealized: Decimal,
    expected: Decimal,
) -> AttributionRow:
    total = price + dividend - fees - taxes - slippage
    residual = expected - total
    if residual != ZERO:
        raise RuntimeError(f"attribution residual is non-zero for {key}: {residual}")
    return AttributionRow(
        key,
        price,
        dividend,
        -fees,
        -taxes,
        -slippage,
        ZERO,
        ZERO,
        total,
        realized,
        unrealized,
        residual,
    )


def _instrument_rows(
    result: BacktestResult, portfolio: AttributionRow
) -> tuple[AttributionRow, ...]:
    notionals: dict[str, Decimal] = {}
    for event in result.events:
        if event.kind is BacktestEventKind.FILL_RECEIVED and event.payload["status"] in {
            "filled",
            "partial",
        }:
            fill = event.payload["fill"]
            key = str(fill["instrument"]["market"]) + ":" + str(fill["instrument"]["symbol"])
            notionals[key] = notionals.get(key, ZERO) + Decimal(str(fill["price"])) * int(
                fill["quantity"]
            )
    if not notionals:
        return ()
    total_notional = sum(notionals.values(), ZERO)
    keys = sorted(notionals)
    rows: list[AttributionRow] = []
    allocated = ZERO
    for index, key in enumerate(keys):
        value = (
            portfolio.total - allocated
            if index == len(keys) - 1
            else portfolio.total * notionals[key] / total_notional
        )
        allocated += value
        rows.append(
            AttributionRow(key, value, ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, value, ZERO, ZERO, ZERO)
        )
    return tuple(rows)


def _drawdowns(
    sessions: list[date], values: list[Decimal]
) -> tuple[list[DrawdownPoint], date | None, date | None, date | None, int, bool]:
    points: list[DrawdownPoint] = []
    peak = values[0]
    peak_index = 0
    maximum = ZERO
    start_index: int | None = None
    trough_index: int | None = None
    peak_value_for_max: Decimal | None = None
    for index, (session, value) in enumerate(zip(sessions, values, strict=True)):
        if value >= peak:
            peak = value
            peak_index = index
        drawdown = value / peak - Decimal("1")
        points.append(DrawdownPoint(session, drawdown))
        if drawdown < maximum:
            maximum = drawdown
            start_index = peak_index
            trough_index = index
            peak_value_for_max = peak
    if start_index is None or trough_index is None or peak_value_for_max is None:
        return points, None, None, None, 0, False
    recovery_index = next(
        (
            index
            for index in range(trough_index + 1, len(values))
            if values[index] >= peak_value_for_max
        ),
        None,
    )
    end_index = recovery_index if recovery_index is not None else len(values) - 1
    return (
        points,
        sessions[start_index],
        sessions[trough_index],
        sessions[recovery_index] if recovery_index is not None else None,
        end_index - start_index,
        recovery_index is None,
    )


def _slippage_cost(events: list[Any]) -> Decimal:
    return sum((_event_slippage(event) for event in events), ZERO)


def _cumulative_price_movement(
    result: BacktestResult,
    positions_market_value: Decimal,
    *,
    through: date | None = None,
) -> Decimal:
    buys = sells = slippage = ZERO
    for event in result.events:
        if event.kind is not BacktestEventKind.FILL_RECEIVED:
            continue
        if through is not None and event.occurred_at.date() > through:
            continue
        if event.payload.get("status") not in {"filled", "partial"}:
            continue
        fill = event.payload["fill"]
        gross = Decimal(str(fill["price"])) * int(fill["quantity"])
        if fill["side"] == "buy":
            buys += gross
        else:
            sells += gross
        slippage += _event_slippage(event)
    return positions_market_value + sells - buys + slippage


def _standard_deviation(values: list[float], ddof: int) -> float | None:
    if len(values) <= ddof:
        return None
    return statistics.stdev(values) if ddof == 1 else statistics.pstdev(values)


def _event_slippage(event: Any) -> Decimal:
    if event.payload.get("status") not in {"filled", "partial"}:
        return ZERO
    fill = event.payload["fill"]
    reference = Decimal(str(event.payload["reference_price"]))
    executed = Decimal(str(fill["price"]))
    quantity = int(fill["quantity"])
    return (
        (executed - reference) * quantity
        if fill["side"] == "buy"
        else (reference - executed) * quantity
    )


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
