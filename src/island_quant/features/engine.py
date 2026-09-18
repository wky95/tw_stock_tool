"""Point-in-time feature materialization over pinned market-data snapshots."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import polars as pl

from island_quant.data.availability import AvailabilityPolicy
from island_quant.data.calendar import CanonicalTradingCalendar
from island_quant.features.contracts import FeatureContract, FeatureSetManifest

_LABEL_COLUMNS = {"label", "label_value", "raw_return", "benchmark_return"}
_IMPLEMENTATION_INPUTS: dict[str, set[str]] = {
    "reversal_1": {"close"},
    "momentum_5": {"close"},
    "momentum_20": {"close"},
    "momentum_60": {"close"},
    "realized_volatility_20": {"close"},
    "downside_volatility_20": {"close"},
    "average_traded_value_20": {"traded_value"},
    "volume_activity_proxy_20": {"volume"},
    "amihud_illiquidity_20": {"close", "traded_value"},
    "overnight_return": {"open", "close"},
    "intraday_return": {"open", "close"},
    "price_to_moving_average_20": {"close"},
    "distance_from_rolling_high_20": {"close", "high"},
    "volume_surprise_20": {"volume"},
}


@dataclass(frozen=True, slots=True)
class FeatureBuildResult:
    frame: pl.DataFrame
    feature_set: FeatureSetManifest


class FeatureEngine:
    def __init__(self, availability_policy: AvailabilityPolicy) -> None:
        self.availability_policy = availability_policy

    def materialize(
        self,
        prices: pl.DataFrame,
        universe: pl.DataFrame,
        calendar: CanonicalTradingCalendar,
        contracts: tuple[FeatureContract, ...],
        feature_set: FeatureSetManifest,
        *,
        input_dataset_version: str,
        universe_version: str,
        computed_at: datetime | None = None,
    ) -> FeatureBuildResult:
        if input_dataset_version in {"", "latest", "current"}:
            raise ValueError("feature materialization requires a pinned dataset version")
        if any(set(contract.required_input_columns) & _LABEL_COLUMNS for contract in contracts):
            raise ValueError("feature contracts cannot access label columns")
        for contract in contracts:
            implementation_inputs = _IMPLEMENTATION_INPUTS.get(contract.name)
            if implementation_inputs is None:
                raise ValueError(f"feature implementation is not registered: {contract.name}")
            if not implementation_inputs.issubset(contract.required_input_columns):
                raise ValueError(
                    f"feature {contract.name} implementation attempted undeclared inputs"
                )
        expected_contracts = {
            (str(item["name"]), str(item["version"])): item
            for item in feature_set.contracts
        }
        if any(
            expected_contracts.get((contract.name, contract.version))
            != contract.to_manifest()
            for contract in contracts
        ):
            raise ValueError("materialized contracts do not match the feature-set manifest")
        if "price_view" in prices.columns and set(prices["price_view"].unique()) != {
            "canonical_unadjusted"
        }:
            raise ValueError("features require canonical_unadjusted prices")
        built_at = computed_at or self._deterministic_computed_at(prices)
        price_map = {
            (str(row["instrument_id"]), row["trade_date"]): row for row in prices.to_dicts()
        }
        market_sessions = {
            market: calendar.trading_dates(market)
            for market in calendar.sessions["market"].unique().to_list()
        }
        rows: list[dict[str, Any]] = []
        for member in universe.sort(["trade_date", "instrument_id"]).to_dicts():
            if not bool(member["eligible"]):
                continue
            day: date = member["trade_date"]
            instrument_id = str(member["instrument_id"])
            market = str(member["market"])
            calendar_market = market if market in market_sessions else "tw"
            sessions = market_sessions.get(calendar_market, [])
            if day not in sessions:
                continue
            session_index = sessions.index(day)
            for contract in contracts:
                value, reason = self._compute(
                    contract, instrument_id, day, session_index, sessions, price_map
                )
                decision_time = self.availability_policy.decision_time(day)
                rows.append(
                    {
                        "instrument_id": instrument_id,
                        "market": market,
                        "decision_date": day,
                        "decision_time": decision_time,
                        "feature_name": contract.name,
                        "feature_version": contract.version,
                        "value": value,
                        "valid": reason is None,
                        "invalid_reason": reason,
                        "input_dataset_version": input_dataset_version,
                        "universe_version": universe_version,
                        "availability_policy_version": self.availability_policy.version,
                        "feature_set_version": feature_set.version,
                        "feature_set_checksum": feature_set.checksum,
                        "computed_at": built_at,
                    }
                )
        return FeatureBuildResult(pl.DataFrame(rows, infer_schema_length=None), feature_set)

    def _compute(
        self,
        contract: FeatureContract,
        instrument_id: str,
        day: date,
        session_index: int,
        sessions: list[date],
        price_map: dict[tuple[str, date], dict[str, Any]],
    ) -> tuple[float | None, str | None]:
        start = session_index - contract.minimum_observations + 1
        if start < 0:
            return None, "insufficient_lookback"
        required_dates = sessions[start : session_index + 1]
        history = [price_map.get((instrument_id, item)) for item in required_dates]
        if any(row is None for row in history):
            return None, "provider_missing_or_suspended_session"
        observations = [row for row in history if row is not None]
        decision_time = self.availability_policy.decision_time(day)
        if any(
            row.get("available_at") is None or row["available_at"] > decision_time
            for row in observations
        ):
            return None, "observation_not_available_at_decision"
        if any(float(row.get("volume", 0)) <= 0 for row in observations):
            return None, "zero_volume_or_suspended_observation"
        if any(
            row.get(column) is None
            for row in observations
            for column in contract.required_input_columns
        ):
            return None, "missing_required_input"
        if any(
            not math.isfinite(float(row[column]))
            for row in observations
            for column in contract.required_input_columns
        ):
            return None, "non_finite_input"
        price_columns = set(contract.required_input_columns) & {"open", "high", "low", "close"}
        if any(float(row[column]) <= 0 for row in observations for column in price_columns):
            return None, "non_positive_price_input"
        if "traded_value" in contract.required_input_columns and any(
            float(row["traded_value"]) <= 0 for row in observations
        ):
            return None, "non_positive_traded_value"
        calculation_rows = [
            {column: row[column] for column in contract.required_input_columns}
            for row in observations
        ]
        try:
            value = _calculate(contract.name, calculation_rows)
        except (ArithmeticError, ValueError, statistics.StatisticsError):
            return None, "invalid_numeric_input"
        if not math.isfinite(value):
            return None, "non_finite_result"
        return value, None

    @staticmethod
    def _deterministic_computed_at(prices: pl.DataFrame) -> datetime:
        value: Any = (
            prices["ingested_at"].max()
            if "ingested_at" in prices.columns
            else prices["available_at"].max()
        )
        if not isinstance(value, datetime):
            raise ValueError("feature input requires a deterministic timestamp")
        return value


def _returns(rows: list[dict[str, Any]]) -> list[float]:
    closes = [float(row["close"]) for row in rows]
    return [closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes))]


def _calculate(name: str, rows: list[dict[str, Any]]) -> float:
    current = rows[-1]
    if name == "reversal_1":
        return -(float(current["close"]) / float(rows[-2]["close"]) - 1.0)
    if name.startswith("momentum_"):
        return float(current["close"]) / float(rows[0]["close"]) - 1.0
    if name == "realized_volatility_20":
        return statistics.stdev(_returns(rows))
    if name == "downside_volatility_20":
        returns = _returns(rows)
        return math.sqrt(sum(min(value, 0.0) ** 2 for value in returns) / len(returns))
    if name == "average_traded_value_20":
        return statistics.fmean(float(row["traded_value"]) for row in rows)
    if name == "volume_activity_proxy_20":
        average = statistics.fmean(float(row["volume"]) for row in rows)
        return float(current["volume"]) / average
    if name == "amihud_illiquidity_20":
        returns = _returns(rows)
        traded_values = [float(row["traded_value"]) for row in rows[1:]]
        return statistics.fmean(
            abs(return_value) / traded_value
            for return_value, traded_value in zip(returns, traded_values, strict=True)
        )
    if name == "overnight_return":
        return float(current["open"]) / float(rows[-2]["close"]) - 1.0
    if name == "intraday_return":
        return float(current["close"]) / float(current["open"]) - 1.0
    if name == "price_to_moving_average_20":
        average = statistics.fmean(float(row["close"]) for row in rows)
        return float(current["close"]) / average - 1.0
    if name == "distance_from_rolling_high_20":
        return float(current["close"]) / max(float(row["high"]) for row in rows) - 1.0
    if name == "volume_surprise_20":
        average = statistics.fmean(float(row["volume"]) for row in rows[:-1])
        return float(current["volume"]) / average - 1.0
    raise ValueError(f"feature implementation is not registered: {name}")
