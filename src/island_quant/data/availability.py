"""Versioned point-in-time availability rules for Taiwan market data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import polars as pl

MARKET_TIMEZONE = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True, slots=True)
class AvailabilityPolicy:
    """Convert publication assumptions into explicit, reproducible timestamps."""

    version: str = "tw-daily-v1"
    market_close: time = time(13, 30)
    publication_time: time = time(17, 30)
    finalization_buffer: timedelta = timedelta(minutes=30)

    def event_time(self, trading_day: date) -> datetime:
        return datetime.combine(trading_day, self.market_close, tzinfo=MARKET_TIMEZONE)

    def published_at(self, trading_day: date) -> datetime:
        return datetime.combine(trading_day, self.publication_time, tzinfo=MARKET_TIMEZONE)

    def available_at(self, trading_day: date) -> datetime:
        return self.published_at(trading_day) + self.finalization_buffer

    def decision_time(self, trading_day: date) -> datetime:
        return self.available_at(trading_day)

    def filter_available(self, frame: pl.DataFrame, decision_time: datetime) -> pl.DataFrame:
        if decision_time.tzinfo is None or decision_time.utcoffset() is None:
            raise ValueError("decision_time must be timezone-aware")
        return frame.filter(pl.col("available_at").is_not_null()).filter(
            pl.col("available_at") <= decision_time
        )

    def decision_snapshot(
        self, frame: pl.DataFrame, decision_time: datetime, *, quality_passed: bool
    ) -> pl.DataFrame:
        """Admit observations only after quality checks and the availability cutoff."""
        if not quality_passed:
            raise RuntimeError("daily data quality checks have not passed")
        return self.filter_available(frame, decision_time)
