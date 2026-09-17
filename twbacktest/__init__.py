"""A small, dependency-free Taiwan stock backtesting toolkit."""

from .engine import BacktestConfig, BacktestError, run_backtest
from .data import DataError, fetch_tpex_daily, fetch_twse_daily, parse_csv_text
from .domain import AlertRule, Bar, Instrument, Quote
from .monitoring import MonitoringService

__all__ = [
    "BacktestConfig",
    "BacktestError",
    "AlertRule",
    "Bar",
    "DataError",
    "Instrument",
    "MonitoringService",
    "Quote",
    "fetch_twse_daily",
    "fetch_tpex_daily",
    "parse_csv_text",
    "run_backtest",
]
