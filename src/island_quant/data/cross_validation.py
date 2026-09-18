"""Explicit unit-aware comparisons between normalized and reference observations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VolumeComparison:
    normalized_shares: int
    reference_raw_value: float
    reference_raw_unit: str
    reference_shares: int
    absolute_difference_shares: int
    relative_difference: float


def compare_volume_shares(
    normalized_shares: int,
    reference_raw_value: float,
    reference_raw_unit: str,
) -> VolumeComparison:
    if normalized_shares < 0 or reference_raw_value < 0:
        raise ValueError("volume values cannot be negative")
    multipliers = {"shares": 1, "thousand_shares": 1000}
    if reference_raw_unit not in multipliers:
        raise ValueError(f"unsupported volume unit: {reference_raw_unit}")
    reference_shares = round(reference_raw_value * multipliers[reference_raw_unit])
    difference = abs(normalized_shares - reference_shares)
    denominator = max(normalized_shares, reference_shares, 1)
    return VolumeComparison(
        normalized_shares=normalized_shares,
        reference_raw_value=reference_raw_value,
        reference_raw_unit=reference_raw_unit,
        reference_shares=reference_shares,
        absolute_difference_shares=difference,
        relative_difference=difference / denominator,
    )
