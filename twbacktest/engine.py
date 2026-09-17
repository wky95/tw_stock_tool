from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from typing import Any

from .domain import Bar
from .strategies import StrategyParameterError, strategy_registry


class BacktestError(ValueError):
    pass


@dataclass
class BacktestConfig:
    initial_cash: float = 1_000_000
    commission_rate: float = 0.001425
    commission_discount: float = 0.28
    min_commission: float = 20
    tax_rate: float = 0.003
    slippage_bps: float = 0
    lot_size: int = 1000
    force_liquidate: bool = True

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> "BacktestConfig":
        allowed = cls.__dataclass_fields__.keys()
        config = cls(**{key: values[key] for key in allowed if key in values})
        if config.initial_cash <= 0:
            raise BacktestError("初始資金必須大於 0")
        for name in ("commission_rate", "commission_discount", "min_commission", "tax_rate", "slippage_bps"):
            if getattr(config, name) < 0:
                raise BacktestError(f"{name} 不可為負數")
        if config.lot_size < 1:
            raise BacktestError("交易單位必須至少為 1 股")
        return config


def _commission(value: float, config: BacktestConfig) -> float:
    return max(config.min_commission, value * config.commission_rate * config.commission_discount)


def _round(value: float | None, digits: int = 2):
    return None if value is None or not math.isfinite(value) else round(value, digits)


def run_backtest(
    bars: list[Bar],
    strategy: str,
    params: dict[str, Any] | None = None,
    config: BacktestConfig | None = None,
) -> dict[str, Any]:
    config = config or BacktestConfig()
    if len(bars) < 2:
        raise BacktestError("至少需要 2 筆日 K 資料")
    if any(bars[i].date >= bars[i + 1].date for i in range(len(bars) - 1)):
        raise BacktestError("日 K 日期必須遞增且不可重複")

    try:
        output = strategy_registry.get(strategy).generate(bars, params or {})
    except StrategyParameterError as exc:
        raise BacktestError(str(exc)) from exc
    targets, indicators = output.targets, output.indicators
    cash = float(config.initial_cash)
    shares = 0
    entry: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []
    executions: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []

    for i, bar in enumerate(bars):
        desired = targets[i - 1] if i > 0 else 0
        if desired and shares == 0:
            price = bar.open * (1 + config.slippage_bps / 10_000)
            unit_budget = price * config.lot_size * (1 + config.commission_rate * config.commission_discount)
            lots = int(cash // unit_budget)
            while lots > 0:
                candidate_shares = lots * config.lot_size
                value = candidate_shares * price
                fee = _commission(value, config)
                if value + fee <= cash:
                    break
                lots -= 1
            if lots > 0:
                shares = lots * config.lot_size
                value = shares * price
                fee = _commission(value, config)
                cash -= value + fee
                entry = {"date": bar.date, "price": price, "shares": shares, "cost": value + fee, "fee": fee}
                executions.append({"date": bar.date, "side": "BUY", "price": _round(price), "shares": shares, "fee": _round(fee)})
        elif not desired and shares > 0 and entry:
            price = bar.open * (1 - config.slippage_bps / 10_000)
            value = shares * price
            fee = _commission(value, config)
            tax = value * config.tax_rate
            proceeds = value - fee - tax
            cash += proceeds
            pnl = proceeds - entry["cost"]
            trades.append({
                "entry_date": entry["date"], "exit_date": bar.date,
                "entry_price": _round(entry["price"]), "exit_price": _round(price),
                "shares": shares, "pnl": _round(pnl), "return_pct": _round(pnl / entry["cost"] * 100),
                "fees_tax": _round(entry["fee"] + fee + tax), "exit_reason": "訊號",
            })
            executions.append({"date": bar.date, "side": "SELL", "price": _round(price), "shares": shares, "fee": _round(fee), "tax": _round(tax)})
            shares = 0
            entry = None

        equity_curve.append({"date": bar.date, "equity": cash + shares * bar.close, "close": bar.close})

    if shares > 0 and entry and config.force_liquidate:
        bar = bars[-1]
        price = bar.close * (1 - config.slippage_bps / 10_000)
        value = shares * price
        fee = _commission(value, config)
        tax = value * config.tax_rate
        proceeds = value - fee - tax
        cash += proceeds
        pnl = proceeds - entry["cost"]
        trades.append({
            "entry_date": entry["date"], "exit_date": bar.date,
            "entry_price": _round(entry["price"]), "exit_price": _round(price),
            "shares": shares, "pnl": _round(pnl), "return_pct": _round(pnl / entry["cost"] * 100),
            "fees_tax": _round(entry["fee"] + fee + tax), "exit_reason": "期末平倉",
        })
        executions.append({"date": bar.date, "side": "SELL", "price": _round(price), "shares": shares, "fee": _round(fee), "tax": _round(tax)})
        shares = 0
        entry = None
        equity_curve[-1]["equity"] = cash

    equities = [point["equity"] for point in equity_curve]
    daily_returns = [equities[i] / equities[i - 1] - 1 for i in range(1, len(equities)) if equities[i - 1] > 0]
    years = max((len(bars) - 1) / 252, 1 / 252)
    total_return = equities[-1] / config.initial_cash - 1
    annual_return = (equities[-1] / config.initial_cash) ** (1 / years) - 1 if equities[-1] > 0 else -1
    volatility = statistics.stdev(daily_returns) * math.sqrt(252) if len(daily_returns) > 1 else 0
    sharpe = statistics.mean(daily_returns) / statistics.stdev(daily_returns) * math.sqrt(252) if len(daily_returns) > 1 and statistics.stdev(daily_returns) else 0
    peak = equities[0]
    max_drawdown = 0.0
    for value in equities:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1)
    wins = [trade for trade in trades if trade["pnl"] > 0]
    gross_profit = sum(trade["pnl"] for trade in trades if trade["pnl"] > 0)
    gross_loss = -sum(trade["pnl"] for trade in trades if trade["pnl"] < 0)
    profit_factor = gross_profit / gross_loss if gross_loss else (None if not gross_profit else 999.99)

    first_close = bars[0].close
    for point in equity_curve:
        point["equity"] = _round(point["equity"])
        point["benchmark"] = _round(config.initial_cash * point["close"] / first_close)

    series_indicators = {
        name: [_round(value, 4) for value in values] for name, values in indicators.items()
    }
    return {
        "summary": {
            "initial_cash": _round(config.initial_cash), "final_equity": _round(equities[-1]),
            "total_return_pct": _round(total_return * 100), "annual_return_pct": _round(annual_return * 100),
            "max_drawdown_pct": _round(max_drawdown * 100), "annual_volatility_pct": _round(volatility * 100),
            "sharpe": _round(sharpe), "trade_count": len(trades),
            "win_rate_pct": _round(len(wins) / len(trades) * 100) if trades else None,
            "profit_factor": _round(profit_factor),
            "buy_hold_pct": _round((bars[-1].close / bars[0].close - 1) * 100),
        },
        "equity_curve": equity_curve,
        "trades": trades,
        "executions": executions,
        "indicators": series_indicators,
        "bars": [bar.to_dict() for bar in bars],
        "config": asdict(config),
        "assumptions": ["收盤產生訊號，下一交易日開盤成交", "僅做多、不融資", "未計入股利與除權息還原"],
    }
