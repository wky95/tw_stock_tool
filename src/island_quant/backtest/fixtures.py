"""Small, offline, deterministic fixtures for engineering verification only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from island_quant.backtest.contracts import BacktestSession, PredictionObservation
from island_quant.backtest.engine import BacktestConfig
from island_quant.backtest.policies import FeeTaxPolicy, FillPolicy, RiskPolicy, SettlementPolicy
from island_quant.domain.models import Instrument, Market
from island_quant.execution.simulation import ExecutionQuote

TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True, slots=True)
class SyntheticBacktestFixture:
    instruments: tuple[Instrument, ...]
    sessions: tuple[BacktestSession, ...]
    config: BacktestConfig


def synthetic_backtest_fixture(run_id: str = "phase2-synthetic-v1") -> SyntheticBacktestFixture:
    """Return pinned toy prices; they are not evidence of investable performance."""
    instruments = (
        Instrument("1101", Market.TWSE),
        Instrument("2330", Market.TWSE),
        Instrument("6488", Market.TPEX),
    )
    calendar = tuple(date(2024, 1, 2) + timedelta(days=offset) for offset in range(8))
    calendar = tuple(day for day in calendar if day.weekday() < 5)
    price_rows = (
        (("40", "40.5"), ("100", "101"), ("60", "59.5")),
        (("40.6", "41"), ("101.5", "100.5"), ("59.4", "60.2")),
        (("41.2", "40.8"), ("100.4", "102"), ("60.3", "61")),
    )
    sessions: list[BacktestSession] = []
    prior_closes: tuple[Decimal, ...] = (
        Decimal("40"),
        Decimal("100"),
        Decimal("60"),
    )
    for index, day in enumerate(calendar[:3]):
        decision_date = date(2023, 12, 29) if index == 0 else calendar[index - 1]
        decision_time = datetime.combine(decision_date, time(18), tzinfo=TAIPEI)
        execution_time = datetime.combine(day, time(9), tzinfo=TAIPEI)
        close_time = datetime.combine(day, time(13, 30), tzinfo=TAIPEI)
        quotes = tuple(
            ExecutionQuote(
                instrument,
                execution_time,
                Decimal(price_rows[index][instrument_index][0]),
                Decimal(price_rows[index][instrument_index][1]),
                100_000,
            )
            for instrument_index, instrument in enumerate(instruments)
        )
        predictions = tuple(
            PredictionObservation(
                instrument,
                Decimal(index + instrument_index + 1) / Decimal("100"),
                decision_time - timedelta(minutes=1),
                "synthetic-prediction-v1",
            )
            for instrument_index, instrument in enumerate(instruments)
        )
        sessions.append(
            BacktestSession(
                day,
                decision_time,
                execution_time,
                close_time,
                quotes,
                tuple(
                    (instrument.key, prior_closes[instrument_index])
                    for instrument_index, instrument in enumerate(instruments)
                ),
                predictions,
            )
        )
        prior_closes = tuple(quote.close_price for quote in quotes)
    config = BacktestConfig(
        run_id=run_id,
        portfolio_id="synthetic-twd-cash",
        initial_cash=Decimal("100000"),
        trading_calendar=calendar,
        fee_policy=FeeTaxPolicy(),
        settlement_policy=SettlementPolicy(),
        fill_policy=FillPolicy(maximum_volume_participation=Decimal("1")),
        risk_policy=RiskPolicy(maximum_position_weight=Decimal("1")),
        pit_reference_complete=False,
    )
    return SyntheticBacktestFixture(instruments, tuple(sessions), config)
