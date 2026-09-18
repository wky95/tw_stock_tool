"""Auditable OHLCV baseline factor definitions."""

from __future__ import annotations

from island_quant.features.contracts import (
    FeatureContract,
    FeatureRegistry,
    MissingValuePolicy,
)

BASELINE_FEATURE_VERSION = "1.0.0"


def _contract(
    name: str,
    definition: str,
    columns: tuple[str, ...],
    lookback: int,
    minimum: int,
    dataset_version: str,
    expected_direction: str | None = None,
    output_unit: str = "ratio",
) -> FeatureContract:
    return FeatureContract(
        name=name,
        version=BASELINE_FEATURE_VERSION,
        mathematical_definition=definition,
        required_input_columns=columns,
        required_price_view="canonical_unadjusted",
        lookback_sessions=lookback,
        minimum_observations=minimum,
        decision_time_rule="after_close_finalized_t",
        availability_policy_version="tw-daily-v1",
        missing_value_policy=MissingValuePolicy.INVALID,
        winsorization_policy="none",
        normalization_policy="none",
        neutralization_policy="none_no_pit_sector_or_size_data",
        expected_direction=expected_direction or "research_unspecified",
        supported_markets=("twse", "tpex"),
        output_dtype="float64",
        output_unit=output_unit,
        code_version="phase1-slice3-v1",
        dataset_version=dataset_version,
    )


def baseline_contracts(dataset_version: str) -> tuple[FeatureContract, ...]:
    if dataset_version in {"", "latest", "current"}:
        raise ValueError("baseline contracts require a pinned dataset version")
    return (
        _contract("reversal_1", "-(close_t / close_t-1 - 1)", ("close",), 1, 2, dataset_version),
        _contract("momentum_5", "close_t / close_t-5 - 1", ("close",), 5, 6, dataset_version),
        _contract("momentum_20", "close_t / close_t-20 - 1", ("close",), 20, 21, dataset_version),
        _contract("momentum_60", "close_t / close_t-60 - 1", ("close",), 60, 61, dataset_version),
        _contract(
            "realized_volatility_20",
            "sample_std of 20 close returns ending t; requires 21 prices; ddof=1",
            ("close",),
            20,
            21,
            dataset_version,
        ),
        _contract(
            "downside_volatility_20",
            "sqrt(mean(min(return,0)^2)) over 20 returns ending t; requires 21 prices",
            ("close",),
            20,
            21,
            dataset_version,
        ),
        _contract(
            "average_traded_value_20",
            "mean(traded_value[t-19:t]), inclusive of t",
            ("traded_value",),
            20,
            20,
            dataset_version,
            output_unit="TWD",
        ),
        _contract(
            "volume_activity_proxy_20",
            "volume_t / mean(volume[t-19:t]), denominator inclusive of t",
            ("volume",),
            20,
            20,
            dataset_version,
        ),
        _contract(
            "amihud_illiquidity_20",
            "mean(abs(return)/traded_value) over 20 returns ending t; requires 21 prices",
            ("close", "traded_value"),
            20,
            21,
            dataset_version,
            output_unit="inverse_TWD",
        ),
        _contract(
            "overnight_return",
            "open_t / close_t-1 - 1",
            ("open", "close"),
            1,
            2,
            dataset_version,
        ),
        _contract(
            "intraday_return",
            "close_t / open_t - 1",
            ("open", "close"),
            1,
            1,
            dataset_version,
        ),
        _contract(
            "price_to_moving_average_20",
            "close_t / mean(close[t-19:t]) - 1",
            ("close",),
            20,
            20,
            dataset_version,
        ),
        _contract(
            "distance_from_rolling_high_20",
            "close_t / max(high[t-19:t]) - 1, rolling high inclusive of t",
            ("close", "high"),
            20,
            20,
            dataset_version,
        ),
        _contract(
            "volume_surprise_20",
            "volume_t / mean(volume[t-20:t-1]) - 1, denominator excludes t",
            ("volume",),
            20,
            21,
            dataset_version,
        ),
    )


def baseline_registry(dataset_version: str) -> FeatureRegistry:
    registry = FeatureRegistry()
    for contract in baseline_contracts(dataset_version):
        registry.register(contract)
    return registry
