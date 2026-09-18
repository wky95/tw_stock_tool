"""Versioned feature preprocessing without full-period leakage."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

import polars as pl


class UnsupportedNeutralizationError(RuntimeError):
    pass


class FittedTransformation(Protocol):
    version: str
    dataset_version: str
    fit_start: date
    fit_end: date

    def fit(self, frame: pl.DataFrame) -> None: ...

    def transform(self, frame: pl.DataFrame) -> pl.DataFrame: ...

    def parameters(self) -> dict[str, Any]: ...


def add_missing_indicator(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_columns(
        (pl.col("value").is_null() | ~pl.col("valid")).alias("missing_indicator")
    )


def cross_sectional_rank(frame: pl.DataFrame) -> pl.DataFrame:
    """Average-rank ties within a single decision timestamp; preserve invalid rows."""
    groups = _cross_section_groups(frame)
    valid = frame.filter(pl.col("valid") & pl.col("value").is_not_null())
    ranked = valid.with_columns(
        pl.col("value").rank(method="average").over(groups).alias("rank_number"),
        pl.len().over(groups).alias("coverage"),
    ).with_columns(
        pl.when(pl.col("coverage") == 1)
        .then(pl.lit(0.5))
        .otherwise((pl.col("rank_number") - 1) / (pl.col("coverage") - 1))
        .alias("transformed_value")
    )
    key_columns = [*groups, "instrument_id"]
    values = {
        tuple(row[column] for column in key_columns): (
            row["transformed_value"], row["coverage"]
        )
        for row in ranked.select(
            *key_columns, "transformed_value", "coverage"
        ).to_dicts()
    }
    return pl.DataFrame(
        [
            {
                **row,
                "transformed_value": values.get(
                    tuple(row[column] for column in key_columns), (None, 0)
                )[0],
                "coverage": values.get(
                    tuple(row[column] for column in key_columns), (None, 0)
                )[1],
            }
            for row in frame.to_dicts()
        ],
        infer_schema_length=None,
    )


def cross_sectional_zscore(frame: pl.DataFrame) -> pl.DataFrame:
    groups = _cross_section_groups(frame)
    valid = frame.filter(pl.col("valid") & pl.col("value").is_not_null())
    transformed = valid.with_columns(
        pl.col("value").mean().over(groups).alias("daily_mean"),
        pl.col("value").std(ddof=1).over(groups).alias("daily_std"),
    ).with_columns(
        pl.when(pl.col("daily_std") > 0)
        .then((pl.col("value") - pl.col("daily_mean")) / pl.col("daily_std"))
        .otherwise(pl.lit(None, dtype=pl.Float64))
        .alias("transformed_value")
    )
    return transformed


def winsorize_cross_section(frame: pl.DataFrame, lower: float, upper: float) -> pl.DataFrame:
    if not 0 <= lower < upper <= 1:
        raise ValueError("winsorization bounds must satisfy 0 <= lower < upper <= 1")
    groups = _cross_section_groups(frame)
    return frame.with_columns(
        pl.col("value").quantile(lower).over(groups).alias("lower_bound"),
        pl.col("value").quantile(upper).over(groups).alias("upper_bound"),
    ).with_columns(
        pl.col("value")
        .clip(pl.col("lower_bound"), pl.col("upper_bound"))
        .alias("transformed_value")
    )


@dataclass(slots=True)
class RobustScaler:
    version: str
    dataset_version: str
    fit_start: date
    fit_end: date
    median: float | None = None
    mad: float | None = None

    def fit(self, frame: pl.DataFrame) -> None:
        training = frame.filter(
            (pl.col("decision_date") >= self.fit_start)
            & (pl.col("decision_date") <= self.fit_end)
            & pl.col("valid")
            & pl.col("value").is_not_null()
        )["value"].to_list()
        if not training:
            raise ValueError("robust scaler fit period has no valid observations")
        self.median = float(statistics.median(training))
        deviations = [abs(float(value) - self.median) for value in training]
        self.mad = float(statistics.median(deviations))
        if not math.isfinite(self.mad) or self.mad <= 0:
            raise ValueError("robust scaler requires positive finite MAD")

    def transform(self, frame: pl.DataFrame) -> pl.DataFrame:
        if self.median is None or self.mad is None:
            raise RuntimeError("robust scaler must be fitted before transform")
        return frame.with_columns(
            ((pl.col("value") - self.median) / self.mad).alias("transformed_value")
        )

    def parameters(self) -> dict[str, Any]:
        if self.median is None or self.mad is None:
            raise RuntimeError("robust scaler has no fitted parameters")
        return {
            "transformation": "robust_scaler",
            "version": self.version,
            "dataset_version": self.dataset_version,
            "fit_start": self.fit_start.isoformat(),
            "fit_end": self.fit_end.isoformat(),
            "median": self.median,
            "mad": self.mad,
        }


def sector_neutralize(_frame: pl.DataFrame) -> pl.DataFrame:
    raise UnsupportedNeutralizationError(
        "sector neutralization requires point-in-time sector membership"
    )


def size_neutralize(_frame: pl.DataFrame) -> pl.DataFrame:
    raise UnsupportedNeutralizationError(
        "size neutralization requires point-in-time shares outstanding and market cap"
    )


def _cross_section_groups(frame: pl.DataFrame) -> list[str]:
    return [
        column
        for column in ("decision_time", "feature_name", "feature_version")
        if column in frame.columns
    ]
