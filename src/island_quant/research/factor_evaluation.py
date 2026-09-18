"""Cross-sectional factor evaluation with explicit gross-before-costs semantics."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any

import polars as pl

from island_quant.research.completeness import ResearchCompleteness


@dataclass(frozen=True, slots=True)
class FactorEvaluation:
    daily_ic: pl.DataFrame
    summary: dict[str, Any]
    quantile_returns: pl.DataFrame
    yearly_breakdown: pl.DataFrame
    market_breakdown: pl.DataFrame
    ic_decay: pl.DataFrame
    universe_counts: pl.DataFrame
    report_metadata: dict[str, Any]


class FactorEvaluator:
    def __init__(
        self,
        minimum_cross_section: int = 3,
        quantiles: int = 5,
        minimum_icir_observations: int = 20,
    ) -> None:
        if minimum_cross_section < 3 or quantiles < 2:
            raise ValueError("minimum cross section >=3 and quantiles >=2 are required")
        self.minimum_cross_section = minimum_cross_section
        self.quantiles = quantiles
        self.minimum_icir_observations = minimum_icir_observations

    def evaluate(
        self,
        features: pl.DataFrame,
        labels: pl.DataFrame,
        completeness: ResearchCompleteness,
        *,
        feature_artifact_version: str,
        tested_factor_inventory: tuple[dict[str, Any], ...],
        direction: str = "long_high",
        annualization_sessions: int = 252,
    ) -> FactorEvaluation:
        if direction not in {"long_high", "long_low"}:
            raise ValueError("direction must be long_high or long_low")
        if feature_artifact_version in {"", "latest", "current"}:
            raise ValueError("factor reports require a pinned feature artifact")
        if not tested_factor_inventory:
            raise ValueError("tested-factor inventory must include every attempted factor")
        joined = features.filter(pl.col("valid")).join(
            labels.filter(pl.col("valid")),
            on=["instrument_id", "decision_date"],
            how="inner",
            suffix="_label",
        ).filter(pl.col("value").is_not_null() & pl.col("label_value").is_not_null())
        all_horizons = (
            sorted(joined["horizon_sessions"].unique().to_list())
            if "horizon_sessions" in joined.columns and joined.height
            else []
        )
        primary_horizon = all_horizons[0] if all_horizons else None
        primary = (
            joined.filter(pl.col("horizon_sessions") == primary_horizon)
            if primary_horizon is not None
            else joined
        )
        daily_rows: list[dict[str, Any]] = []
        quantile_rows: list[dict[str, Any]] = []
        previous_members: dict[int, set[str]] = {}
        for decision_day in sorted(primary["decision_date"].unique().to_list()):
            day_rows = primary.filter(pl.col("decision_date") == decision_day).sort(
                ["value", "instrument_id"]
            ).to_dicts()
            count = len(day_rows)
            if count < self.minimum_cross_section:
                daily_rows.append(
                    {
                        "decision_date": decision_day,
                        "sample_count": count,
                        "pearson_ic": None,
                        "spearman_ic": None,
                        "valid": False,
                        "invalid_reason": "insufficient_cross_section",
                    }
                )
                continue
            factor_values = [float(row["value"]) for row in day_rows]
            label_values = [float(row["label_value"]) for row in day_rows]
            pearson_ic = _pearson(factor_values, label_values)
            spearman_ic = _pearson(
                _average_ranks(factor_values), _average_ranks(label_values)
            )
            daily_rows.append(
                {
                    "decision_date": decision_day,
                    "sample_count": count,
                    "pearson_ic": pearson_ic,
                    "spearman_ic": spearman_ic,
                    "valid": pearson_ic is not None and spearman_ic is not None,
                    "invalid_reason": (
                        None
                        if pearson_ic is not None and spearman_ic is not None
                        else "constant_cross_section"
                    ),
                }
            )
            groups = _deterministic_quantiles(day_rows, self.quantiles)
            for quantile, members in groups.items():
                returns = [float(row["label_value"]) for row in members]
                current_members = {str(row["instrument_id"]) for row in members}
                prior = previous_members.get(quantile, set())
                membership_turnover = (
                    1.0 - len(current_members & prior) / len(current_members)
                    if prior and current_members
                    else None
                )
                previous_members[quantile] = current_members
                quantile_rows.append(
                    {
                        "decision_date": decision_day,
                        "quantile": quantile,
                        "gross_return": statistics.fmean(returns),
                        "holding_period_return": statistics.fmean(returns),
                        "membership_turnover": membership_turnover,
                        "member_count": len(members),
                        "return_semantics": "gross_before_costs",
                    }
                )
        daily = pl.DataFrame(daily_rows, infer_schema_length=None)
        quantile_frame = pl.DataFrame(quantile_rows, infer_schema_length=None)
        summary = self._summary(
            daily,
            quantile_frame,
            primary,
            features,
            direction,
            annualization_sessions,
            self.minimum_icir_observations,
        )
        return FactorEvaluation(
            daily_ic=daily,
            summary=summary,
            quantile_returns=quantile_frame,
            yearly_breakdown=_breakdown(primary, "year"),
            market_breakdown=_breakdown(primary, "market"),
            ic_decay=_ic_decay(joined, self.minimum_cross_section),
            universe_counts=(
                primary.group_by("decision_date")
                .len()
                .rename({"len": "eligible_feature_label_count"})
                .sort("decision_date")
            ),
            report_metadata={
                **completeness.to_metadata(),
                "title_prefix": (
                    "EXPLORATORY — INCOMPLETE POINT-IN-TIME DATA"
                    if completeness.classification == "exploratory"
                    else "VALIDATED"
                ),
                "direction": direction,
                "quantiles": self.quantiles,
                "quantile_tie_rule": "stable_sort_by_value_then_instrument_id",
                "ic_method": "daily_cross_sectional",
                "icir_annualization_sessions": annualization_sessions,
                "ic_frequency": "daily_decision_date_cross_section",
                "icir_is_annualized": True,
                "icir_invalid_dates": "excluded",
                "daily_ic_observation_count": daily.filter(pl.col("valid")).height,
                "minimum_icir_observations": self.minimum_icir_observations,
                "significance_method": "not_reported",
                "return_semantics": "gross_before_costs",
                "feature_artifact_version": feature_artifact_version,
                "turnover_definition": (
                    "membership_turnover=1-|current_members∩prior_members|/|current_members|; "
                    "new entries count as turnover, exits affect the changed current set, "
                    "invalid rows are excluded, size changes use current count, no price drift"
                ),
                "primary_label_horizon_sessions": primary_horizon,
                "available_label_horizons": all_horizons,
                "tested_factor_inventory": list(tested_factor_inventory),
            },
        )

    @staticmethod
    def _summary(
        daily: pl.DataFrame,
        quantiles: pl.DataFrame,
        joined: pl.DataFrame,
        all_features: pl.DataFrame,
        direction: str,
        annualization: int,
        minimum_icir_observations: int,
    ) -> dict[str, Any]:
        valid = daily.filter(pl.col("valid"))
        pearson = valid["pearson_ic"].drop_nulls().to_list() if valid.height else []
        spearman = valid["spearman_ic"].drop_nulls().to_list() if valid.height else []
        mean_ic = statistics.fmean(pearson) if pearson else None
        standard_deviation = statistics.stdev(pearson) if len(pearson) > 1 else None
        icir = None
        if (
            len(pearson) >= minimum_icir_observations
            and mean_ic is not None
            and standard_deviation is not None
            and standard_deviation != 0
        ):
            icir = mean_ic / standard_deviation * math.sqrt(annualization)
        spread = None
        if quantiles.height:
            pivoted = quantiles.pivot(
                on="quantile", index="decision_date", values="gross_return"
            )
            low_name, high_name = "1", str(quantiles["quantile"].max())
            if low_name in pivoted.columns and high_name in pivoted.columns:
                raw = pivoted[high_name] - pivoted[low_name]
                spread = raw.mean() if direction == "long_high" else (-raw).mean()
        values = [float(value) for value in joined["value"].to_list()]
        return {
            "pearson_ic_mean": mean_ic,
            "spearman_ic_mean": statistics.fmean(spearman) if spearman else None,
            "ic_standard_deviation": standard_deviation,
            "icir": icir,
            "icir_available": icir is not None,
            "ic_positive_ratio": (
                sum(value > 0 for value in pearson) / len(pearson) if pearson else None
            ),
            "coverage": (
                joined.height / all_features.height if all_features.height else 0.0
            ),
            "valid_observation_count": joined.height,
            "total_feature_observation_count": all_features.height,
            "missing_rate": (
                1.0 - joined.height / all_features.height if all_features.height else None
            ),
            "factor_distribution": (
                {
                    "minimum": min(values),
                    "median": statistics.median(values),
                    "maximum": max(values),
                }
                if values
                else {}
            ),
            "top_minus_bottom_mean": spread,
            "mean_membership_turnover": (
                quantiles["membership_turnover"].drop_nulls().mean()
                if quantiles.height
                else None
            ),
            "tested_factor_inventory_required": True,
        }


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean, right_mean = statistics.fmean(left), statistics.fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True))
    left_scale = math.sqrt(sum((x - left_mean) ** 2 for x in left))
    right_scale = math.sqrt(sum((y - right_mean) ** 2 for y in right))
    return numerator / (left_scale * right_scale) if left_scale and right_scale else None


def _average_ranks(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    position = 0
    while position < len(indexed):
        end = position + 1
        while end < len(indexed) and indexed[end][1] == indexed[position][1]:
            end += 1
        average = (position + 1 + end) / 2
        for original_index, _ in indexed[position:end]:
            ranks[original_index] = average
        position = end
    return ranks


def _deterministic_quantiles(
    rows: list[dict[str, Any]], quantiles: int
) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    count = len(rows)
    for index, row in enumerate(
        sorted(rows, key=lambda item: (float(item["value"]), str(item["instrument_id"])))
    ):
        quantile = min(quantiles, index * quantiles // count + 1)
        grouped.setdefault(quantile, []).append(row)
    return grouped


def _breakdown(frame: pl.DataFrame, kind: str) -> pl.DataFrame:
    if not frame.height:
        return pl.DataFrame()
    rows: list[dict[str, Any]] = []
    for decision_day in sorted(frame["decision_date"].unique().to_list()):
        day_frame = frame.filter(pl.col("decision_date") == decision_day)
        keys = (
            [decision_day.year]
            if kind == "year"
            else sorted(day_frame[kind].unique().to_list())
        )
        for key in keys:
            cross_section = (
                day_frame
                if kind == "year"
                else day_frame.filter(pl.col(kind) == key)
            )
            values = cross_section["value"].to_list()
            labels = cross_section["label_value"].to_list()
            rows.append(
                {
                    kind: key,
                    "decision_date": decision_day,
                    "sample_count": len(values),
                    "pearson_ic": _pearson(values, labels),
                    "spearman_ic": _pearson(
                        _average_ranks(values), _average_ranks(labels)
                    ),
                }
            )
    return pl.DataFrame(rows, infer_schema_length=None).group_by(kind).agg(
        pl.col("sample_count").sum().alias("count"),
        pl.col("pearson_ic").drop_nulls().mean(),
        pl.col("spearman_ic").drop_nulls().mean(),
    ).sort(kind)


def _ic_decay(frame: pl.DataFrame, minimum_cross_section: int) -> pl.DataFrame:
    if "horizon_sessions" not in frame.columns or not frame.height:
        return pl.DataFrame()
    rows: list[dict[str, Any]] = []
    for horizon in sorted(frame["horizon_sessions"].unique().to_list()):
        horizon_frame = frame.filter(pl.col("horizon_sessions") == horizon)
        daily_values: list[float] = []
        for decision_day in horizon_frame["decision_date"].unique().to_list():
            cross_section = horizon_frame.filter(pl.col("decision_date") == decision_day)
            if cross_section.height < minimum_cross_section:
                continue
            correlation = _pearson(
                cross_section["value"].to_list(), cross_section["label_value"].to_list()
            )
            if correlation is not None:
                daily_values.append(correlation)
        rows.append(
            {
                "horizon_sessions": horizon,
                "daily_ic_mean": statistics.fmean(daily_values) if daily_values else None,
                "valid_day_count": len(daily_values),
            }
        )
    return pl.DataFrame(rows, infer_schema_length=None)
