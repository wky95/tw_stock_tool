"""Leakage-resistant forward-return labels aligned to executable sessions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from island_quant.data.availability import AvailabilityPolicy
from island_quant.data.calendar import CanonicalTradingCalendar

MARKET_TIMEZONE = ZoneInfo("Asia/Taipei")


class LabelKind(StrEnum):
    NEXT_SESSION_OPEN_TO_OPEN = "next_session_open_to_open"
    NEXT_SESSION_OPEN_TO_CLOSE = "next_session_open_to_close"
    N_SESSION_OPEN_TO_OPEN = "n_session_open_to_open"
    BENCHMARK_RELATIVE = "benchmark_relative"
    CROSS_SECTIONAL_RANK = "cross_sectional_rank"


@dataclass(frozen=True, slots=True)
class LabelSpec:
    kind: LabelKind
    version: str
    horizon_sessions: int = 1
    base_kind: LabelKind = LabelKind.N_SESSION_OPEN_TO_OPEN

    def __post_init__(self) -> None:
        if not self.version or self.horizon_sessions < 1:
            raise ValueError("label version and positive horizon_sessions are required")
        if self.base_kind in {LabelKind.BENCHMARK_RELATIVE, LabelKind.CROSS_SECTIONAL_RANK}:
            raise ValueError("base_kind must be an absolute-return label")


class ForwardReturnLabelBuilder:
    def __init__(self, availability_policy: AvailabilityPolicy) -> None:
        self.availability_policy = availability_policy

    def build(
        self,
        spec: LabelSpec,
        prices: pl.DataFrame,
        universe: pl.DataFrame,
        calendar: CanonicalTradingCalendar,
        *,
        dataset_version: str,
        price_view_version: str,
        quality_passed: bool,
        quality_report_version: str,
        instruments: pl.DataFrame | None = None,
        benchmark_prices: pl.DataFrame | None = None,
    ) -> pl.DataFrame:
        if not quality_passed:
            raise RuntimeError("labels require a completed, passing daily-data quality report")
        if not quality_report_version:
            raise ValueError("quality_report_version is required")
        if "price_view" in prices.columns and set(prices["price_view"].unique()) != {
            "canonical_unadjusted"
        }:
            raise ValueError("labels require the canonical_unadjusted price view")
        price_map = {
            (str(row["instrument_id"]), row["trade_date"]): row for row in prices.to_dicts()
        }
        instrument_map = (
            {str(row["instrument_id"]): row for row in instruments.to_dicts()}
            if instruments is not None
            else {}
        )
        benchmark_map = self._benchmark_map(benchmark_prices)
        rows: list[dict[str, Any]] = []
        eligible = universe.filter(pl.col("eligible")).sort(["trade_date", "instrument_id"])
        for member in eligible.to_dicts():
            rows.append(
                self._build_one(
                    spec,
                    member,
                    price_map,
                    calendar,
                    instrument_map,
                    benchmark_map,
                    dataset_version,
                    price_view_version,
                    quality_report_version,
                )
            )
        result = pl.DataFrame(rows, infer_schema_length=None)
        if spec.kind is LabelKind.CROSS_SECTIONAL_RANK and result.height:
            result = self._add_cross_sectional_ranks(result)
        return result

    def _build_one(
        self,
        spec: LabelSpec,
        member: dict[str, Any],
        price_map: dict[tuple[str, date], dict[str, Any]],
        calendar: CanonicalTradingCalendar,
        instrument_map: dict[str, dict[str, Any]],
        benchmark_map: dict[tuple[str, date], dict[str, Any]],
        dataset_version: str,
        price_view_version: str,
        quality_report_version: str,
    ) -> dict[str, Any]:
        decision_date: date = member["trade_date"]
        instrument_id = str(member["instrument_id"])
        market = str(member.get("market", "tw"))
        calendar_markets = set(calendar.sessions["market"].unique())
        calendar_market = market if market in calendar_markets else "tw"
        sessions = calendar.trading_dates(calendar_market)
        future = [day for day in sessions if day > decision_date]
        base_kind = (
            spec.base_kind
            if spec.kind
            in {
                LabelKind.BENCHMARK_RELATIVE,
                LabelKind.CROSS_SECTIONAL_RANK,
            }
            else spec.kind
        )
        horizon = 1 if base_kind is LabelKind.NEXT_SESSION_OPEN_TO_OPEN else spec.horizon_sessions
        needed = 1 if base_kind is LabelKind.NEXT_SESSION_OPEN_TO_CLOSE else horizon + 1
        entry_date = future[0] if future else None
        exit_date = (
            future[0]
            if base_kind is LabelKind.NEXT_SESSION_OPEN_TO_CLOSE and future
            else (future[horizon] if len(future) > horizon else None)
        )
        invalid_reason: str | None = None
        entry = price_map.get((instrument_id, entry_date)) if entry_date else None
        exit_row = price_map.get((instrument_id, exit_date)) if exit_date else None
        if len(future) < needed:
            invalid_reason = "insufficient_future_sessions"
        elif self._crosses_listing_boundary(
            instrument_map.get(instrument_id), entry_date, exit_date
        ):
            invalid_reason = "listing_or_delisting_boundary"
        elif entry is None or exit_row is None:
            invalid_reason = "missing_or_suspended_observation"
        elif (
            float(entry["open"]) <= 0
            or int(entry.get("volume", 0)) <= 0
            or entry.get("is_tradable") is False
            or entry.get("limit_locked") is True
        ):
            invalid_reason = "unexecutable_entry_observation"
        exit_field = "close" if base_kind is LabelKind.NEXT_SESSION_OPEN_TO_CLOSE else "open"
        if (
            exit_row is not None
            and invalid_reason is None
            and (
                float(exit_row[exit_field]) <= 0
                or exit_row.get("is_tradable") is False
                or exit_row.get("limit_locked") is True
            )
        ):
            invalid_reason = "unexecutable_exit_observation"
        raw_return: float | None = None
        benchmark_return: float | None = None
        if invalid_reason is None and entry is not None and exit_row is not None:
            raw_return = float(exit_row[exit_field]) / float(entry["open"]) - 1.0
            if (
                spec.kind is LabelKind.BENCHMARK_RELATIVE
                and entry_date is not None
                and exit_date is not None
            ):
                benchmark_entry = benchmark_map.get((market, entry_date))
                benchmark_exit = benchmark_map.get((market, exit_date))
                if benchmark_entry is None or benchmark_exit is None:
                    invalid_reason = "missing_benchmark_observation"
                    raw_return = None
                else:
                    benchmark_return = (
                        float(benchmark_exit[exit_field]) / float(benchmark_entry["open"]) - 1.0
                    )
        label_value = (
            raw_return - benchmark_return
            if raw_return is not None and benchmark_return is not None
            else raw_return
        )
        earliest_execution_time = (
            datetime.combine(entry_date, time(9, 0), tzinfo=MARKET_TIMEZONE) if entry_date else None
        )
        label_interval_end = (
            datetime.combine(
                exit_date,
                time(13, 30) if exit_field == "close" else time(9, 0),
                tzinfo=MARKET_TIMEZONE,
            )
            if exit_date
            else None
        )
        return {
            "decision_date": decision_date,
            "decision_time": self.availability_policy.decision_time(decision_date),
            "instrument_id": instrument_id,
            "market": market,
            "label_kind": spec.kind.value,
            "return_definition": self._return_definition(spec, base_kind),
            "horizon_sessions": horizon,
            "label_value": label_value,
            "raw_return": raw_return,
            "benchmark_return": benchmark_return,
            "entry_observation": self._observation(entry_date, entry, "open"),
            "exit_observation": self._observation(exit_date, exit_row, exit_field),
            "earliest_execution_time": earliest_execution_time,
            "label_interval_start": earliest_execution_time,
            "label_interval_end": label_interval_end,
            "dataset_version": dataset_version,
            "price_view_version": price_view_version,
            "label_version": spec.version,
            "availability_policy_version": self.availability_policy.version,
            "quality_report_version": quality_report_version,
            "valid": invalid_reason is None,
            "invalid_reason": invalid_reason,
        }

    @staticmethod
    def _observation(day: date | None, row: dict[str, Any] | None, field: str) -> str | None:
        if day is None or row is None:
            return None
        return json.dumps(
            {
                "trade_date": day.isoformat(),
                "field": field,
                "value": row.get(field),
                "available_at": str(row.get("available_at")),
            },
            sort_keys=True,
        )

    @staticmethod
    def _return_definition(spec: LabelSpec, base_kind: LabelKind) -> str:
        exit_field = "close" if base_kind is LabelKind.NEXT_SESSION_OPEN_TO_CLOSE else "open"
        definition = f"gross_{exit_field}_over_next_open_minus_one"
        if spec.kind is LabelKind.BENCHMARK_RELATIVE:
            return f"asset_{definition}_minus_aligned_benchmark_{definition}"
        if spec.kind is LabelKind.CROSS_SECTIONAL_RANK:
            return f"eligible_universe_percentile_rank_of_{definition}"
        return definition

    @staticmethod
    def _crosses_listing_boundary(
        instrument: dict[str, Any] | None, entry_date: date | None, exit_date: date | None
    ) -> bool:
        if instrument is None or entry_date is None or exit_date is None:
            return False
        listing_date = instrument.get("listing_date")
        delisting_date = instrument.get("delisting_date")
        return bool(
            (listing_date is not None and entry_date < listing_date)
            or (delisting_date is not None and exit_date >= delisting_date)
        )

    @staticmethod
    def _benchmark_map(
        benchmark_prices: pl.DataFrame | None,
    ) -> dict[tuple[str, date], dict[str, Any]]:
        if benchmark_prices is None:
            return {}
        return {(str(row["market"]), row["trade_date"]): row for row in benchmark_prices.to_dicts()}

    @staticmethod
    def _add_cross_sectional_ranks(frame: pl.DataFrame) -> pl.DataFrame:
        valid = frame.filter(pl.col("valid"))
        if not valid.height:
            return frame
        ranks = valid.with_columns(
            pl.col("raw_return").rank(method="average").over("decision_date").alias("rank_number"),
            pl.len().over("decision_date").alias("rank_count"),
        ).with_columns(
            pl.when(pl.col("rank_count") == 1)
            .then(pl.lit(0.5))
            .otherwise((pl.col("rank_number") - 1) / (pl.col("rank_count") - 1))
            .alias("rank_value")
        )
        rank_map = {
            (row["decision_date"], row["instrument_id"]): row["rank_value"]
            for row in ranks.select("decision_date", "instrument_id", "rank_value").to_dicts()
        }
        return pl.DataFrame(
            [
                {
                    **row,
                    "label_value": rank_map.get((row["decision_date"], row["instrument_id"])),
                }
                for row in frame.to_dicts()
            ],
            infer_schema_length=None,
        )
