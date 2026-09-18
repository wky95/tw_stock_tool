"""Explicit sample-weight policies for cross-sectional panels."""

from __future__ import annotations

from enum import StrEnum

import polars as pl


class SampleWeightPolicy(StrEnum):
    EQUAL_OBSERVATION = "equal_weight_per_observation"
    EQUAL_DECISION_DATE = "equal_total_weight_per_decision_date"


def sample_weights(frame: pl.DataFrame, policy: SampleWeightPolicy) -> list[float]:
    if not frame.height:
        return []
    if policy is SampleWeightPolicy.EQUAL_OBSERVATION:
        return [1.0] * frame.height
    counts = {
        row["decision_date"]: int(row["len"])
        for row in frame.group_by("decision_date").len().to_dicts()
    }
    return [1.0 / counts[day] for day in frame["decision_date"].to_list()]
