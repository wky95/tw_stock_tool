"""Deterministic engineering-fixture strategies that emit target positions only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from island_quant.backtest.contracts import BacktestSession
from island_quant.domain.models import Instrument, TargetPosition

ZERO = Decimal("0")


def _target(
    strategy_id: str,
    instrument: Instrument,
    session: BacktestSession,
    weight: Decimal,
    reason: str,
) -> TargetPosition:
    return TargetPosition(
        strategy_id=strategy_id,
        instrument=instrument,
        generated_at=session.decision_time,
        target_weight=weight,
        reason=reason,
    )


@dataclass(frozen=True, slots=True)
class BuyAndHoldFixtureStrategy:
    instrument: Instrument
    entry_date: date
    strategy_id: str = "fixture-buy-and-hold"
    version: str = "1.0.0"

    def targets(self, session: BacktestSession) -> tuple[TargetPosition, ...]:
        if session.trade_date != self.entry_date:
            return ()
        return tuple(
            _target(
                self.strategy_id,
                instrument,
                session,
                Decimal("1") if instrument.key == self.instrument.key else ZERO,
                "deterministic synthetic buy-and-hold fixture",
            )
            for instrument in session.instruments
        )


@dataclass(frozen=True, slots=True)
class EqualWeightFixtureStrategy:
    strategy_id: str = "fixture-equal-weight"
    version: str = "1.0.0"

    def targets(self, session: BacktestSession) -> tuple[TargetPosition, ...]:
        if not session.instruments:
            return ()
        weight = Decimal("1") / len(session.instruments)
        return tuple(
            _target(
                self.strategy_id,
                instrument,
                session,
                (
                    Decimal("1") - weight * (len(session.instruments) - 1)
                    if index == len(session.instruments) - 1
                    else weight
                ),
                "deterministic synthetic equal-weight fixture",
            )
            for index, instrument in enumerate(session.instruments)
        )


@dataclass(frozen=True, slots=True)
class PredictionTopKFixtureStrategy:
    top_k: int
    strategy_id: str = "fixture-prediction-top-k"
    version: str = "1.0.0"

    def __post_init__(self) -> None:
        if self.top_k < 1:
            raise ValueError("top_k must be positive")

    def targets(self, session: BacktestSession) -> tuple[TargetPosition, ...]:
        predictions = {item.instrument.key: item for item in session.predictions}
        if len(predictions) < self.top_k:
            raise ValueError("prediction fixture has fewer observations than top_k")
        selected = {
            item.instrument.key
            for item in sorted(
                predictions.values(), key=lambda item: (-item.value, item.instrument.key)
            )[: self.top_k]
        }
        weight = Decimal("1") / self.top_k
        return tuple(
            _target(
                self.strategy_id,
                instrument,
                session,
                weight if instrument.key in selected else ZERO,
                "deterministic pinned prediction top-k fixture",
            )
            for instrument in session.instruments
        )
