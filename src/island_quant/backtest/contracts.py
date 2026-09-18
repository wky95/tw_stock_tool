"""Backtest input contracts and strategy port."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from island_quant.domain.models import Instrument, TargetPosition
from island_quant.execution.simulation import ExecutionQuote

TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True, slots=True)
class PredictionObservation:
    instrument: Instrument
    value: Decimal
    available_at: datetime
    artifact_version: str

    def __post_init__(self) -> None:
        if self.available_at.tzinfo is None or self.available_at.utcoffset() is None:
            raise ValueError("prediction available_at must be timezone-aware")
        if not self.artifact_version:
            raise ValueError("prediction artifact version is required")


@dataclass(frozen=True, slots=True)
class BacktestSession:
    trade_date: date
    decision_time: datetime
    execution_time: datetime
    close_time: datetime
    quotes: tuple[ExecutionQuote, ...]
    decision_prices: tuple[tuple[str, Decimal], ...]
    predictions: tuple[PredictionObservation, ...] = ()

    def __post_init__(self) -> None:
        for value in (self.decision_time, self.execution_time, self.close_time):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("session timestamps must be timezone-aware")
            if value.tzinfo != TAIPEI:
                raise ValueError("Taiwan market session timestamps must use Asia/Taipei")
        if not self.decision_time < self.execution_time < self.close_time:
            raise ValueError("session chronology must be decision < execution < close")
        if self.decision_time.date() >= self.trade_date:
            raise ValueError("decision must occur after a prior close and before trade date")
        if (
            self.execution_time.date() != self.trade_date
            or self.close_time.date() != self.trade_date
        ):
            raise ValueError("execution and close timestamps must match trade date")
        if any(quote.event_time != self.execution_time for quote in self.quotes):
            raise ValueError("execution quotes must align exactly to session execution time")
        if len({quote.instrument.key for quote in self.quotes}) != len(self.quotes):
            raise ValueError("session contains duplicate instrument quotes")
        if any(item.available_at > self.decision_time for item in self.predictions):
            raise ValueError("future prediction is unavailable at decision time")

    @property
    def instruments(self) -> tuple[Instrument, ...]:
        return tuple(quote.instrument for quote in self.quotes)

    def decision_price(self, instrument: Instrument) -> Decimal:
        prices = dict(self.decision_prices)
        try:
            return prices[instrument.key]
        except KeyError as exc:
            raise ValueError(f"missing decision-time price for {instrument.key}") from exc


class BacktestStrategy(Protocol):
    strategy_id: str
    version: str

    def targets(self, session: BacktestSession) -> tuple[TargetPosition, ...]: ...
