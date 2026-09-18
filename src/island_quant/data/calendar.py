"""Canonical market sessions and explicit missing-observation classifications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

import polars as pl


class SessionStatus(StrEnum):
    TRADING_DAY = "trading_day"
    HOLIDAY = "holiday"
    UNEXPECTED_CLOSURE = "unexpected_closure"


class ObservationStatus(StrEnum):
    OBSERVED = "observed"
    MARKET_CLOSED = "market_closed"
    NOT_YET_LISTED = "not_yet_listed"
    DELISTED = "delisted"
    SUSPENDED = "suspended"
    PROVIDER_MISSING = "provider_missing"
    ZERO_VOLUME = "zero_volume_observation"


@dataclass(frozen=True, slots=True)
class CanonicalTradingCalendar:
    sessions: pl.DataFrame
    version: str

    def __post_init__(self) -> None:
        required = {"trade_date", "market", "status"}
        if not required.issubset(self.sessions.columns):
            raise ValueError(f"calendar requires columns: {sorted(required)}")
        duplicates = (
            self.sessions.group_by(["trade_date", "market"]).len().filter(pl.col("len") > 1)
        )
        if duplicates.height:
            raise ValueError("duplicate calendar session key")
        if "timezone" in self.sessions.columns and set(self.sessions["timezone"]) != {
            "Asia/Taipei"
        }:
            raise ValueError("Taiwan calendar timezone must be Asia/Taipei")

    @classmethod
    def from_trading_dates(
        cls, trading_dates: list[date], start: date, end: date, version: str, market: str = "tw"
    ) -> CanonicalTradingCalendar:
        known = set(trading_dates)
        rows = []
        cursor = start
        while cursor <= end:
            rows.append(
                {
                    "trade_date": cursor,
                    "market": market,
                    "status": (
                        SessionStatus.TRADING_DAY.value
                        if cursor in known
                        else SessionStatus.HOLIDAY.value
                    ),
                    "timezone": "Asia/Taipei",
                }
            )
            cursor += timedelta(days=1)
        return cls(pl.DataFrame(rows), version)

    def trading_dates(self, market: str = "tw") -> list[date]:
        return (
            self.sessions.filter(
                (pl.col("market") == market) & (pl.col("status") == SessionStatus.TRADING_DAY.value)
            )
            .sort("trade_date")["trade_date"]
            .to_list()
        )

    def next_session(self, day: date, market: str = "tw") -> date | None:
        return next(
            (candidate for candidate in self.trading_dates(market) if candidate > day), None
        )

    def previous_session(self, day: date, market: str = "tw") -> date | None:
        candidates = [candidate for candidate in self.trading_dates(market) if candidate < day]
        return candidates[-1] if candidates else None

    def classify_observation(
        self,
        day: date,
        listing_date: date | None,
        delisting_date: date | None,
        *,
        has_observation: bool,
        volume: int | None = None,
        suspended: bool = False,
        market: str = "tw",
    ) -> ObservationStatus:
        if day not in self.trading_dates(market):
            return ObservationStatus.MARKET_CLOSED
        if listing_date is None or day < listing_date:
            return ObservationStatus.NOT_YET_LISTED
        if delisting_date is not None and day > delisting_date:
            return ObservationStatus.DELISTED
        if suspended:
            return ObservationStatus.SUSPENDED
        if not has_observation:
            return ObservationStatus.PROVIDER_MISSING
        if volume == 0:
            return ObservationStatus.ZERO_VOLUME
        return ObservationStatus.OBSERVED
