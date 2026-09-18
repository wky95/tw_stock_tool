"""Versioned point-in-time eligible-universe construction."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

INCOMPLETE_UNIVERSE_WARNING = (
    "INCOMPLETE POINT-IN-TIME UNIVERSE — RESULTS NOT VALID FOR PRODUCTION CONCLUSIONS"
)


class IncompleteUniverseError(RuntimeError):
    """Raised when incomplete reference history is used without explicit override."""


@dataclass(frozen=True, slots=True)
class UniverseBuild:
    membership: pl.DataFrame
    metadata: pl.DataFrame


def require_research_complete(metadata: pl.DataFrame, allow_incomplete: bool = False) -> None:
    if metadata.height != 1:
        raise ValueError("universe metadata must contain exactly one row")
    if not bool(metadata.row(0, named=True)["is_research_complete"]) and not allow_incomplete:
        raise IncompleteUniverseError(INCOMPLETE_UNIVERSE_WARNING)


@dataclass(frozen=True, slots=True)
class UniversePolicy:
    version: str
    minimum_listing_days: int
    trailing_median_window: int
    minimum_trailing_median_traded_value: Decimal
    minimum_lookback_observations: int

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("universe policy version is required")
        if self.minimum_listing_days < 0:
            raise ValueError("minimum_listing_days cannot be negative")
        if self.trailing_median_window <= 0 or self.minimum_lookback_observations <= 0:
            raise ValueError("universe lookback settings must be positive")

    def build(
        self, instruments: pl.DataFrame, prices: pl.DataFrame, calendar: pl.DataFrame
    ) -> pl.DataFrame:
        return self.build_with_metadata(
            instruments, prices, calendar, reference_dataset_version="unversioned"
        ).membership

    def build_with_metadata(
        self,
        instruments: pl.DataFrame,
        prices: pl.DataFrame,
        calendar: pl.DataFrame,
        reference_dataset_version: str,
    ) -> UniverseBuild:
        dates = sorted(calendar.get_column("trade_date").to_list())
        price_rows: dict[tuple[str, object], dict[str, Any]] = {
            (row["instrument_id"], row["trade_date"]): row for row in prices.to_dicts()
        }
        history: dict[str, list[dict[str, Any]]] = {}
        rows: list[dict[str, object]] = []
        for trading_day in dates:
            for instrument in instruments.to_dicts():
                symbol = str(instrument["instrument_id"])
                reasons: list[str] = []
                if instrument["security_type"] != "ordinary_share":
                    reasons.append("not_ordinary_share")
                if instrument["market"] not in {"twse", "tpex"}:
                    reasons.append("unknown_market")
                listing_date = instrument["listing_date"]
                delisting_date = instrument["delisting_date"]
                if listing_date is None or trading_day < listing_date:
                    reasons.append("not_yet_listed")
                if delisting_date is not None and trading_day >= delisting_date:
                    reasons.append("delisted")
                active_sessions = history.setdefault(symbol, [])
                if len(active_sessions) < self.minimum_listing_days:
                    reasons.append("minimum_listing_days")
                bar = price_rows.get((symbol, trading_day))
                if bar is None:
                    reasons.append("missing_daily_bar")
                elif float(bar["close"]) <= 0:
                    reasons.append("suspended_or_no_price")
                usable_history = [row for row in active_sessions if float(row["close"]) > 0]
                if len(usable_history) < self.minimum_lookback_observations:
                    reasons.append("insufficient_lookback")
                trailing = usable_history[-self.trailing_median_window :]
                if len(trailing) < self.trailing_median_window:
                    reasons.append("insufficient_liquidity_history")
                else:
                    values = sorted(float(row["traded_value"]) for row in trailing)
                    middle = len(values) // 2
                    median = (
                        values[middle]
                        if len(values) % 2
                        else (values[middle - 1] + values[middle]) / 2
                    )
                    if median < float(self.minimum_trailing_median_traded_value):
                        reasons.append("minimum_trailing_median_traded_value")
                decision_time = datetime.combine(
                    trading_day, time(17, 30), tzinfo=ZoneInfo("Asia/Taipei")
                )
                available_at = bar["available_at"] if bar else decision_time
                rows.append(
                    {
                        "trade_date": trading_day,
                        "instrument_id": symbol,
                        "market": instrument["market"],
                        "eligible": not reasons,
                        "exclusion_reasons": json.dumps(sorted(set(reasons))),
                        "policy_version": self.version,
                        "available_at": available_at,
                    }
                )
                if (
                    bar is not None
                    and listing_date is not None
                    and trading_day >= listing_date
                    and (delisting_date is None or trading_day < delisting_date)
                ):
                    active_sessions.append(bar)
        membership = pl.DataFrame(rows, infer_schema_length=None)
        reason_counts: Counter[str] = Counter()
        for row in rows:
            reason_counts.update(json.loads(str(row["exclusion_reasons"])))
        coverage_start = dates[0] if dates else None
        as_of = dates[-1] if dates else None
        if dates:
            relevant = instruments.filter(
                (pl.col("listing_date").is_null() | (pl.col("listing_date") <= as_of))
                & (
                    pl.col("delisting_date").is_null()
                    | (pl.col("delisting_date") > coverage_start)
                )
            )
        else:
            relevant = instruments
        unknown_market_count = relevant.filter(pl.col("market") == "unknown").height
        provisional_count = relevant.filter(
            pl.col("listing_date_quality") == "provisional"
        ).height
        metadata = pl.DataFrame(
            [
                {
                    "universe_policy_version": self.version,
                    "reference_dataset_version": reference_dataset_version,
                    "as_of": as_of,
                    "coverage_start": coverage_start,
                    "known_instrument_count": instruments.height,
                    "unknown_market_count": unknown_market_count,
                    "provisional_listing_date_count": provisional_count,
                    "excluded_by_reason": json.dumps(dict(sorted(reason_counts.items()))),
                    "is_research_complete": unknown_market_count == 0 and provisional_count == 0,
                }
            ],
            infer_schema_length=None,
        )
        return UniverseBuild(membership, metadata)
