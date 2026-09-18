"""Out-of-sample prediction ledger, daily metrics, and date-block bootstrap."""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import polars as pl


@dataclass(frozen=True, slots=True)
class PredictionLedger:
    frame: pl.DataFrame

    @classmethod
    def build(cls, rows: list[dict[str, Any]]) -> PredictionLedger:
        if not rows:
            raise ValueError("OOS prediction ledger cannot be empty")
        frame = pl.DataFrame(rows, infer_schema_length=None).sort(
            ["decision_time", "instrument_id", "fold_id"]
        )
        for row in rows:
            for field in ("decision_time", "fit_cutoff", "created_at"):
                value = row.get(field)
                if not isinstance(value, datetime) or value.tzinfo is None:
                    raise ValueError(f"ledger {field} must be timezone-aware")
        allowed_roles = {"validation", "test", "final_holdout"}
        if set(frame["split_role"].unique()) - allowed_roles:
            raise ValueError("in-sample predictions cannot enter the OOS ledger")
        duplicates = (
            frame.group_by(["instrument_id", "decision_time"])
            .len()
            .filter(pl.col("len") > 1)
        )
        if duplicates.height:
            raise ValueError("duplicate OOS prediction or overlapping test ownership")
        return cls(frame)


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    available: bool
    lower: float | None
    upper: float | None
    seed: int
    block_length: int
    resample_count: int
    unavailable_reason: str | None = None


def evaluate_predictions(ledger: PredictionLedger) -> dict[str, Any]:
    frame = ledger.frame
    daily_rows: list[dict[str, Any]] = []
    for decision_time in sorted(frame["decision_time"].unique().to_list()):
        cross_section = frame.filter(pl.col("decision_time") == decision_time)
        predictions = [float(value) for value in cross_section["prediction"].to_list()]
        targets = [float(value) for value in cross_section["target"].to_list()]
        daily_rows.append(
            {
                "decision_time": decision_time,
                "sample_count": len(predictions),
                "pearson_ic": _correlation(predictions, targets),
                "spearman_ic": _correlation(
                    _average_ranks(predictions), _average_ranks(targets)
                ),
            }
        )
    daily = pl.DataFrame(daily_rows, infer_schema_length=None)
    valid_pearson = daily["pearson_ic"].drop_nulls().to_list()
    valid_spearman = daily["spearman_ic"].drop_nulls().to_list()
    errors = [
        float(prediction) - float(target)
        for prediction, target in zip(
            frame["prediction"].to_list(), frame["target"].to_list(), strict=True
        )
    ]
    mean_ic = statistics.fmean(valid_pearson) if valid_pearson else None
    standard_deviation = (
        statistics.stdev(valid_pearson) if len(valid_pearson) > 1 else None
    )
    quantile_returns, quantile_spread, membership_turnover = _quantile_metrics(frame)
    return {
        "daily_ic": daily,
        "mean_pearson_ic": mean_ic,
        "mean_spearman_ic": (
            statistics.fmean(valid_spearman) if valid_spearman else None
        ),
        "ic_standard_deviation": standard_deviation,
        "icir": _annualized_icir(mean_ic, standard_deviation, len(valid_pearson)),
        "positive_ic_ratio": (
            sum(value > 0 for value in valid_pearson) / len(valid_pearson)
            if valid_pearson
            else None
        ),
        "mae": statistics.fmean(abs(value) for value in errors) if errors else None,
        "rmse": math.sqrt(statistics.fmean(value**2 for value in errors)) if errors else None,
        "prediction_coverage": frame.height,
        "prediction_mean": statistics.fmean(frame["prediction"].to_list()),
        "prediction_std": (
            statistics.stdev(frame["prediction"].to_list()) if frame.height > 1 else 0.0
        ),
        "daily_ic_observation_count": len(valid_pearson),
        "quantile_returns": quantile_returns,
        "top_minus_bottom_spread": quantile_spread,
        "prediction_membership_turnover": membership_turnover,
        "return_semantics": "gross_before_costs",
    }


def block_bootstrap_daily_spearman(
    ledger: PredictionLedger,
    *,
    seed: int,
    block_length: int,
    resample_count: int,
) -> BootstrapResult:
    metrics = evaluate_predictions(ledger)
    daily: pl.DataFrame = metrics["daily_ic"]
    values = daily["spearman_ic"].drop_nulls().to_list()
    if block_length < 1 or resample_count < 1:
        raise ValueError("bootstrap block length and resample count must be positive")
    if len(values) < max(3, block_length * 2):
        return BootstrapResult(
            False,
            None,
            None,
            seed,
            block_length,
            resample_count,
            "insufficient_decision_date_blocks",
        )
    random_generator = random.Random(seed)
    estimates: list[float] = []
    for _ in range(resample_count):
        sample: list[float] = []
        while len(sample) < len(values):
            start = random_generator.randrange(0, len(values) - block_length + 1)
            sample.extend(values[start : start + block_length])
        estimates.append(statistics.fmean(sample[: len(values)]))
    estimates.sort()
    return BootstrapResult(
        True,
        _percentile(estimates, 0.025),
        _percentile(estimates, 0.975),
        seed,
        block_length,
        resample_count,
    )


def _correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2:
        return None
    left_mean, right_mean = statistics.fmean(left), statistics.fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True))
    denominator = math.sqrt(sum((x - left_mean) ** 2 for x in left)) * math.sqrt(
        sum((y - right_mean) ** 2 for y in right)
    )
    return numerator / denominator if denominator else None


def _annualized_icir(
    mean_ic: float | None, standard_deviation: float | None, observations: int
) -> float | None:
    if observations < 20 or mean_ic is None or standard_deviation in {None, 0.0}:
        return None
    assert standard_deviation is not None
    return mean_ic / standard_deviation * math.sqrt(252)


def _average_ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(ordered):
        end = cursor + 1
        while end < len(ordered) and ordered[end][1] == ordered[cursor][1]:
            end += 1
        rank = (cursor + 1 + end) / 2
        for index, _ in ordered[cursor:end]:
            ranks[index] = rank
        cursor = end
    return ranks


def _percentile(values: list[float], probability: float) -> float:
    position = probability * (len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    fraction = position - lower
    return values[lower] * (1 - fraction) + values[upper] * fraction


def _quantile_metrics(
    frame: pl.DataFrame, quantiles: int = 5
) -> tuple[pl.DataFrame, float | None, float | None]:
    rows: list[dict[str, Any]] = []
    spreads: list[float] = []
    turnovers: list[float] = []
    prior_top: set[str] | None = None
    for decision_time in sorted(frame["decision_time"].unique().to_list()):
        cross_section = frame.filter(pl.col("decision_time") == decision_time).sort(
            ["prediction", "instrument_id"]
        ).to_dicts()
        count = len(cross_section)
        if count < quantiles:
            continue
        groups: dict[int, list[dict[str, Any]]] = {}
        for index, row in enumerate(cross_section):
            quantile = min(quantiles, index * quantiles // count + 1)
            groups.setdefault(quantile, []).append(row)
        returns: dict[int, float] = {}
        for quantile, members in groups.items():
            gross_return = statistics.fmean(float(row["target"]) for row in members)
            returns[quantile] = gross_return
            rows.append(
                {
                    "decision_time": decision_time,
                    "quantile": quantile,
                    "gross_return": gross_return,
                    "member_count": len(members),
                }
            )
        if 1 in returns and quantiles in returns:
            spreads.append(returns[quantiles] - returns[1])
        top = {str(row["instrument_id"]) for row in groups.get(quantiles, [])}
        if prior_top is not None and top:
            turnovers.append(1 - len(top & prior_top) / len(top))
        prior_top = top
    return (
        pl.DataFrame(rows, infer_schema_length=None),
        statistics.fmean(spreads) if spreads else None,
        statistics.fmean(turnovers) if turnovers else None,
    )
