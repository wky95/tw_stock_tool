"""Fold-local preprocessing fitted strictly on training samples."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

import polars as pl

from island_quant.ml.dataset import FeatureMatrixSchema


class ImputationPolicy(StrEnum):
    REJECT = "reject_missing"
    TRAIN_MEDIAN = "training_median"


@dataclass(slots=True)
class FoldPreprocessor:
    version: str
    feature_schema: FeatureMatrixSchema
    imputation_policy: ImputationPolicy
    winsor_lower: float = 0.01
    winsor_upper: float = 0.99
    medians: list[float] | None = None
    lower_bounds: list[float] | None = None
    upper_bounds: list[float] | None = None
    means: list[float] | None = None
    standard_deviations: list[float] | None = None
    fit_start: date | None = None
    fit_end: date | None = None

    def fit(self, frame: pl.DataFrame, feature_names: tuple[str, ...]) -> None:
        self.feature_schema.validate_columns(feature_names)
        if not frame.height:
            raise ValueError("preprocessor cannot fit an empty training fold")
        vectors = frame["feature_vector"].to_list()
        width = len(self.feature_schema.names)
        columns = [[vector[index] for vector in vectors] for index in range(width)]
        medians: list[float] = []
        lowers: list[float] = []
        uppers: list[float] = []
        means: list[float] = []
        deviations: list[float] = []
        for values in columns:
            observed = [float(value) for value in values if value is not None]
            if not observed:
                raise ValueError("training fold has an entirely missing feature")
            if any(not math.isfinite(value) for value in observed):
                raise ValueError("training fold contains non-finite feature values")
            median = float(statistics.median(observed))
            if self.imputation_policy is ImputationPolicy.REJECT and len(observed) != len(values):
                raise ValueError("missing feature rejected by preprocessing policy")
            filled = [float(value) if value is not None else median for value in values]
            lower = _quantile(filled, self.winsor_lower)
            upper = _quantile(filled, self.winsor_upper)
            clipped = [min(max(value, lower), upper) for value in filled]
            mean = statistics.fmean(clipped)
            deviation = statistics.stdev(clipped) if len(clipped) > 1 else 0.0
            medians.append(median)
            lowers.append(lower)
            uppers.append(upper)
            means.append(mean)
            deviations.append(deviation if deviation > 0 else 1.0)
        self.medians = medians
        self.lower_bounds = lowers
        self.upper_bounds = uppers
        self.means = means
        self.standard_deviations = deviations
        fit_start: Any = frame["decision_date"].min()
        fit_end: Any = frame["decision_date"].max()
        if not isinstance(fit_start, date) or not isinstance(fit_end, date):
            raise ValueError("preprocessing fit dates are invalid")
        self.fit_start = fit_start
        self.fit_end = fit_end

    def transform(
        self, frame: pl.DataFrame, feature_names: tuple[str, ...]
    ) -> list[list[float]]:
        self.feature_schema.validate_columns(feature_names)
        if any(
            item is None
            for item in (
                self.medians,
                self.lower_bounds,
                self.upper_bounds,
                self.means,
                self.standard_deviations,
            )
        ):
            raise RuntimeError("preprocessor must be fitted on training data")
        assert self.medians is not None
        assert self.lower_bounds is not None
        assert self.upper_bounds is not None
        assert self.means is not None
        assert self.standard_deviations is not None
        transformed: list[list[float]] = []
        for vector in frame["feature_vector"].to_list():
            row: list[float] = []
            for index, raw in enumerate(vector):
                if raw is None:
                    if self.imputation_policy is ImputationPolicy.REJECT:
                        raise ValueError("missing feature rejected during transform")
                    value = self.medians[index]
                else:
                    value = float(raw)
                if not math.isfinite(value):
                    raise ValueError("non-finite feature rejected during transform")
                clipped = min(
                    max(value, self.lower_bounds[index]), self.upper_bounds[index]
                )
                row.append(
                    (clipped - self.means[index]) / self.standard_deviations[index]
                )
            transformed.append(row)
        return transformed

    def parameters(self) -> dict[str, Any]:
        if self.fit_start is None or self.fit_end is None:
            raise RuntimeError("preprocessor has not been fitted")
        return {
            "version": self.version,
            "feature_names": self.feature_schema.names,
            "feature_dtypes": self.feature_schema.dtypes,
            "imputation_policy": self.imputation_policy.value,
            "winsor_lower": self.winsor_lower,
            "winsor_upper": self.winsor_upper,
            "medians": self.medians,
            "lower_bounds": self.lower_bounds,
            "upper_bounds": self.upper_bounds,
            "means": self.means,
            "standard_deviations": self.standard_deviations,
            "fit_start": self.fit_start.isoformat(),
            "fit_end": self.fit_end.isoformat(),
        }


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("quantile requires observations")
    position = probability * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction
