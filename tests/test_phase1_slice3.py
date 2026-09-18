from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from island_quant.cli import main
from island_quant.data.availability import AvailabilityPolicy
from island_quant.data.calendar import CanonicalTradingCalendar
from island_quant.features.baseline import BASELINE_FEATURE_VERSION, baseline_registry
from island_quant.features.contracts import FeatureRegistry, FeatureRegistryError
from island_quant.features.engine import FeatureEngine
from island_quant.features.materialization import FeatureMaterializer
from island_quant.features.preprocessing import (
    RobustScaler,
    UnsupportedNeutralizationError,
    add_missing_indicator,
    cross_sectional_rank,
    sector_neutralize,
)
from island_quant.research.completeness import ResearchCompleteness
from island_quant.research.factor_evaluation import FactorEvaluator
from island_quant.storage.local import LocalArtifactStore
from island_quant.storage.provenance import CodeProvenance

POLICY = AvailabilityPolicy()
COMPUTED_AT = datetime(2024, 4, 1, tzinfo=UTC)


def sessions(count: int = 70) -> list[date]:
    result: list[date] = []
    cursor = date(2024, 1, 2)
    while len(result) < count:
        if cursor.weekday() < 5:
            result.append(cursor)
        cursor += timedelta(days=1)
    return result


def market_fixture(symbols: tuple[str, ...] = ("1111",), count: int = 70):
    days = sessions(count)
    rows = []
    members = []
    for symbol_index, symbol in enumerate(symbols):
        for index, day in enumerate(days):
            close = 100.0 + index * (symbol_index + 1)
            rows.append(
                {
                    "instrument_id": symbol,
                    "trade_date": day,
                    "open": close - 0.5,
                    "high": close + 1,
                    "low": close - 1,
                    "close": close,
                    "volume": 1000 + index,
                    "traded_value": (1000 + index) * close,
                    "available_at": POLICY.available_at(day),
                    "ingested_at": COMPUTED_AT,
                    "price_view": "canonical_unadjusted",
                }
            )
            members.append(
                {
                    "instrument_id": symbol,
                    "trade_date": day,
                    "market": "twse",
                    "eligible": True,
                }
            )
    calendar = CanonicalTradingCalendar.from_trading_dates(
        days, days[0], days[-1], "calendar-v1", market="twse"
    )
    return pl.DataFrame(rows), pl.DataFrame(members), calendar


def feature_build(
    prices: pl.DataFrame,
    universe: pl.DataFrame,
    calendar: CanonicalTradingCalendar,
    names: tuple[str, ...],
):
    registry = baseline_registry("prices-v1")
    contracts = tuple(registry.get(name, BASELINE_FEATURE_VERSION) for name in names)
    feature_set = registry.build_feature_set(
        "baseline-v1",
        tuple((item.name, item.version) for item in contracts),
        "prices-v1",
    )
    return FeatureEngine(POLICY).materialize(
        prices,
        universe,
        calendar,
        contracts,
        feature_set,
        input_dataset_version="prices-v1",
        universe_version="universe-v1",
        computed_at=COMPUTED_AT,
    )


def completeness(complete: bool = False) -> ResearchCompleteness:
    return ResearchCompleteness(
        universe_complete=complete,
        unknown_market_count=0 if complete else 1,
        provisional_listing_date_count=0 if complete else 2,
        unsupported_corporate_action_count=0 if complete else 1,
        missing_suspension_status_count=0 if complete else 3,
        dataset_version="prices-v1",
        feature_set_version="baseline-v1",
        label_version="labels-v1",
        availability_policy_version=POLICY.version,
    )


def evaluate_factor(
    evaluator: FactorEvaluator,
    features: pl.DataFrame,
    labels: pl.DataFrame,
    status: ResearchCompleteness | None = None,
):
    return evaluator.evaluate(
        features,
        labels,
        status or completeness(),
        feature_artifact_version="features-artifact-v1",
        tested_factor_inventory=(
            {"feature": "fixture", "status": "evaluated_or_invalid"},
        ),
    )


def test_future_price_change_does_not_change_historical_feature() -> None:
    prices, universe, calendar = market_fixture(count=10)
    first = feature_build(prices, universe, calendar, ("momentum_5",)).frame
    changed = prices.with_columns(
        pl.when(pl.col("trade_date") == sessions(10)[-1])
        .then(pl.lit(9999.0))
        .otherwise(pl.col("close"))
        .alias("close")
    )
    second = feature_build(changed, universe, calendar, ("momentum_5",)).frame
    cutoff = sessions(10)[-2]
    assert first.filter(pl.col("decision_date") <= cutoff).to_dicts() == second.filter(
        pl.col("decision_date") <= cutoff
    ).to_dicts()


def test_future_universe_membership_does_not_change_historical_rank() -> None:
    decision = datetime(2024, 1, 2, 18, tzinfo=POLICY.available_at(date(2024, 1, 2)).tzinfo)
    base = pl.DataFrame(
        [
            {"instrument_id": "1", "decision_time": decision, "value": 1.0, "valid": True},
            {"instrument_id": "2", "decision_time": decision, "value": 2.0, "valid": True},
        ]
    )
    future = pl.DataFrame(
        [
            {
                "instrument_id": "3",
                "decision_time": decision + timedelta(days=1),
                "value": 100.0,
                "valid": True,
            }
        ]
    )
    first = cross_sectional_rank(base)
    second = cross_sectional_rank(base.vstack(future)).filter(pl.col("decision_time") == decision)
    assert first["transformed_value"].to_list() == second["transformed_value"].to_list()


def test_rolling_window_uses_trading_sessions_and_not_calendar_days() -> None:
    prices, universe, calendar = market_fixture(count=8)
    frame = feature_build(prices, universe, calendar, ("momentum_5",)).frame
    target = frame.filter(pl.col("decision_date") == sessions(8)[5]).row(0, named=True)
    assert target["valid"] is True
    assert target["value"] == pytest.approx(105 / 100 - 1)


def test_insufficient_lookback_fails_closed() -> None:
    prices, universe, calendar = market_fixture(count=5)
    frame = feature_build(prices, universe, calendar, ("momentum_5",)).frame
    assert set(frame["valid"]) == {False}
    assert set(frame["invalid_reason"]) == {"insufficient_lookback"}


@pytest.mark.parametrize(
    ("feature_name", "required_prices"),
    [
        ("momentum_5", 6),
        ("realized_volatility_20", 21),
        ("downside_volatility_20", 21),
        ("average_traded_value_20", 20),
        ("volume_activity_proxy_20", 20),
        ("distance_from_rolling_high_20", 20),
        ("volume_surprise_20", 21),
    ],
)
def test_rolling_boundaries_are_invalid_then_valid_without_window_shift(
    feature_name: str, required_prices: int
) -> None:
    prices, universe, calendar = market_fixture(count=required_prices + 1)
    frame = feature_build(prices, universe, calendar, (feature_name,)).frame.sort(
        "decision_date"
    )
    assert frame.row(required_prices - 2, named=True)["valid"] is False
    assert frame.row(required_prices - 1, named=True)["valid"] is True
    assert frame.row(required_prices, named=True)["valid"] is True
    if feature_name == "momentum_5":
        assert frame.row(required_prices, named=True)["value"] == pytest.approx(106 / 101 - 1)
    if feature_name == "volume_surprise_20":
        expected = 1021 / sum(range(1001, 1021)) * 20 - 1
        assert frame.row(required_prices, named=True)["value"] == pytest.approx(expected)


def test_contract_window_metadata_matches_implementations() -> None:
    contracts = {item.name: item for item in baseline_registry("prices-v1").list()}
    assert contracts["momentum_5"].lookback_sessions == 5
    assert contracts["momentum_5"].minimum_observations == 6
    assert contracts["realized_volatility_20"].minimum_observations == 21
    assert contracts["downside_volatility_20"].minimum_observations == 21
    assert "ddof=1" in contracts["realized_volatility_20"].mathematical_definition
    assert "inclusive of t" in contracts["distance_from_rolling_high_20"].mathematical_definition
    assert "denominator excludes t" in contracts["volume_surprise_20"].mathematical_definition
    assert all(item.output_unit and item.expected_direction for item in contracts.values())


def test_numeric_hazards_fail_closed_and_constant_volatility_is_zero() -> None:
    prices, universe, calendar = market_fixture(count=21)
    zero_value = prices.with_columns(
        pl.when(pl.col("trade_date") == sessions(21)[10])
        .then(pl.lit(0.0))
        .otherwise(pl.col("traded_value"))
        .alias("traded_value")
    )
    amihud = feature_build(
        zero_value, universe, calendar, ("amihud_illiquidity_20",)
    ).frame
    assert "non_positive_traded_value" in set(amihud["invalid_reason"].drop_nulls())

    zero_open = prices.with_columns(
        pl.when(pl.col("trade_date") == sessions(21)[-1])
        .then(pl.lit(0.0))
        .otherwise(pl.col("open"))
        .alias("open")
    )
    intraday = feature_build(zero_open, universe, calendar, ("intraday_return",)).frame
    assert intraday.row(-1, named=True)["invalid_reason"] == "non_positive_price_input"

    non_finite = prices.with_columns(
        pl.when(pl.col("trade_date") == sessions(21)[-1])
        .then(pl.lit(float("nan")))
        .otherwise(pl.col("close"))
        .alias("close")
    )
    momentum = feature_build(non_finite, universe, calendar, ("momentum_5",)).frame
    assert momentum.row(-1, named=True)["invalid_reason"] == "non_finite_input"

    constant = prices.with_columns(pl.lit(100.0).alias("close"))
    volatility = feature_build(
        constant, universe, calendar, ("realized_volatility_20",)
    ).frame
    assert volatility.row(-1, named=True)["value"] == 0.0


def test_provider_missing_and_zero_volume_are_not_zero_returns() -> None:
    prices, universe, calendar = market_fixture(count=8)
    missing_day = sessions(8)[4]
    missing = prices.filter(pl.col("trade_date") != missing_day)
    frame = feature_build(missing, universe, calendar, ("momentum_5",)).frame
    assert "provider_missing_or_suspended_session" in set(frame["invalid_reason"].drop_nulls())
    zero = prices.with_columns(
        pl.when(pl.col("trade_date") == missing_day)
        .then(pl.lit(0))
        .otherwise(pl.col("volume"))
        .alias("volume")
    )
    zero_frame = feature_build(zero, universe, calendar, ("reversal_1",)).frame
    assert "zero_volume_or_suspended_observation" in set(
        zero_frame["invalid_reason"].drop_nulls()
    )


def test_registry_rejects_same_version_with_different_definition() -> None:
    contract = baseline_registry("prices-v1").get("reversal_1", BASELINE_FEATURE_VERSION)
    registry = FeatureRegistry()
    registry.register(contract)
    with pytest.raises(FeatureRegistryError, match="conflicting definition"):
        registry.register(replace(contract, mathematical_definition="future_close / close"))


def test_all_baselines_compute_and_implementation_cannot_read_undeclared_column() -> None:
    prices, universe, calendar = market_fixture(count=70)
    registry = baseline_registry("prices-v1")
    names = tuple(contract.name for contract in registry.list())
    frame = feature_build(prices, universe, calendar, names).frame
    final = frame.filter(pl.col("decision_date") == sessions(70)[-1])
    assert len(names) == 14
    assert final.filter(pl.col("valid")).height == 14

    momentum = registry.get("momentum_5", BASELINE_FEATURE_VERSION)
    invalid_contract = replace(momentum, required_input_columns=("open",))
    feature_set = registry.build_feature_set(
        "bad-v1", ((momentum.name, momentum.version),), "prices-v1"
    )
    with pytest.raises(ValueError, match="undeclared inputs"):
        FeatureEngine(POLICY).materialize(
            prices,
            universe,
            calendar,
            (invalid_contract,),
            feature_set,
            input_dataset_version="prices-v1",
            universe_version="universe-v1",
            computed_at=COMPUTED_AT,
        )


def test_feature_set_is_deterministic_and_rejects_latest() -> None:
    registry = baseline_registry("prices-v1")
    first = registry.build_feature_set(
        "set-v1", (("momentum_5", BASELINE_FEATURE_VERSION),), "prices-v1"
    )
    second = registry.build_feature_set(
        "set-v1", (("momentum_5", BASELINE_FEATURE_VERSION),), "prices-v1"
    )
    assert first.checksum == second.checksum
    with pytest.raises(FeatureRegistryError):
        registry.build_feature_set(
            "set-v1", (("momentum_5", BASELINE_FEATURE_VERSION),), "latest"
        )


def test_feature_output_checksum_is_stable_and_old_version_remains(tmp_path: Path) -> None:
    prices, universe, calendar = market_fixture(count=8)
    result = feature_build(prices, universe, calendar, ("momentum_5",))
    store = LocalArtifactStore(
        tmp_path / "raw",
        tmp_path / "normalized",
        tmp_path / "checkpoints",
        tmp_path / "catalog.duckdb",
        provenance_provider=lambda: CodeProvenance("commit", False, "tree"),
    )
    materializer = FeatureMaterializer(store)
    first = materializer.stage(
        result.frame,
        feature_set_version="baseline-v1",
        input_dataset_version="prices-v1",
        universe_version="universe-v1",
        label_version="labels-v1",
        availability_policy_version=POLICY.version,
        completeness=completeness(True),
        feature_set_checksum=result.feature_set.checksum,
    )
    second = materializer.stage(
        result.frame,
        feature_set_version="baseline-v1",
        input_dataset_version="prices-v1",
        universe_version="universe-v1",
        label_version="labels-v1",
        availability_policy_version=POLICY.version,
        completeness=completeness(True),
        feature_set_checksum=result.feature_set.checksum,
    )
    assert first.artifact.checksum == second.artifact.checksum
    manifest = json.loads(first.artifact.manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["source_checksums"]) == {
        "prices-v1",
        "universe-v1",
        "labels-v1",
        POLICY.version,
    }
    materializer.promote(first, validated=True)
    revised = materializer.stage(
        result.frame.with_columns((pl.col("value") * 2).alias("value")),
        feature_set_version="baseline-v1",
        input_dataset_version="prices-v1",
        universe_version="universe-v1",
        label_version="labels-v1",
        availability_policy_version=POLICY.version,
        completeness=completeness(True),
        feature_set_checksum=result.feature_set.checksum,
    )
    assert store.read_dataset_version(first.dataset_name, first.artifact.checksum).height > 0
    assert revised.artifact.checksum != first.artifact.checksum


def test_cross_sectional_rank_handles_ties_and_missing_without_zero_fill() -> None:
    timestamp = POLICY.decision_time(date(2024, 1, 2))
    frame = pl.DataFrame(
        [
            {"instrument_id": "1", "decision_time": timestamp, "value": 1.0, "valid": True},
            {"instrument_id": "2", "decision_time": timestamp, "value": 1.0, "valid": True},
            {"instrument_id": "3", "decision_time": timestamp, "value": 3.0, "valid": True},
            {"instrument_id": "4", "decision_time": timestamp, "value": None, "valid": False},
        ]
    )
    ranked = cross_sectional_rank(frame).sort("instrument_id")
    assert ranked["transformed_value"].to_list() == [0.25, 0.25, 1.0, None]
    assert add_missing_indicator(frame)["missing_indicator"].to_list() == [
        False,
        False,
        False,
        True,
    ]


def evaluation_frames(sign: float = 1.0):
    days = [date(2024, 1, 2), date(2024, 1, 3)]
    features = []
    labels = []
    for day in days:
        for index, symbol in enumerate(("1", "2", "3", "4", "5"), start=1):
            features.append(
                {
                    "instrument_id": symbol,
                    "decision_date": day,
                    "market": "twse" if index < 4 else "tpex",
                    "value": sign * index,
                    "valid": True,
                }
            )
            labels.append(
                {
                    "instrument_id": symbol,
                    "decision_date": day,
                    "label_value": float(index),
                    "horizon_sessions": 1,
                    "valid": True,
                }
            )
    return pl.DataFrame(features), pl.DataFrame(labels)


def test_pearson_spearman_and_factor_direction_match_hand_calculation() -> None:
    features, labels = evaluation_frames()
    positive = evaluate_factor(FactorEvaluator(quantiles=5), features, labels)
    assert positive.summary["pearson_ic_mean"] == pytest.approx(1.0)
    assert positive.summary["spearman_ic_mean"] == pytest.approx(1.0)
    assert positive.summary["icir"] is None
    assert positive.summary["icir_available"] is False
    assert positive.report_metadata["ic_frequency"] == "daily_decision_date_cross_section"
    assert positive.report_metadata["daily_ic_observation_count"] == 2
    assert positive.report_metadata["feature_artifact_version"] == "features-artifact-v1"
    assert "membership_turnover" in positive.quantile_returns.columns
    assert "turnover" not in positive.quantile_returns.columns
    reversed_features, _ = evaluation_frames(-1)
    negative = evaluate_factor(FactorEvaluator(quantiles=5), reversed_features, labels)
    assert negative.summary["pearson_ic_mean"] == pytest.approx(-1.0)
    assert positive.summary["top_minus_bottom_mean"] == pytest.approx(
        -negative.summary["top_minus_bottom_mean"]
    )


def test_quantile_tie_assignment_is_deterministic() -> None:
    features, labels = evaluation_frames()
    features = features.with_columns(pl.lit(1.0).alias("value"))
    first = evaluate_factor(FactorEvaluator(quantiles=5), features, labels)
    second = evaluate_factor(
        FactorEvaluator(quantiles=5), features.reverse(), labels.reverse()
    )
    assert first.quantile_returns.to_dicts() == second.quantile_returns.to_dicts()


def test_small_cross_section_marks_daily_ic_invalid() -> None:
    features, labels = evaluation_frames()
    features = features.filter(pl.col("instrument_id").is_in(["1", "2"]))
    labels = labels.filter(pl.col("instrument_id").is_in(["1", "2"]))
    result = evaluate_factor(FactorEvaluator(), features, labels)
    assert set(result.daily_ic["valid"]) == {False}
    assert set(result.daily_ic["invalid_reason"]) == {"insufficient_cross_section"}


def test_label_horizon_is_preserved_in_ic_decay() -> None:
    features, labels = evaluation_frames()
    result = evaluate_factor(FactorEvaluator(), features, labels)
    assert result.ic_decay.row(0, named=True)["horizon_sessions"] == 1
    assert result.ic_decay.row(0, named=True)["daily_ic_mean"] == pytest.approx(1.0)


def test_incomplete_pit_data_forces_exploratory_and_rejects_validation() -> None:
    status = completeness(False)
    assert status.classification == "exploratory"
    with pytest.raises(RuntimeError, match="PIT reference data incomplete"):
        status.require_validated()
    features, labels = evaluation_frames()
    report = evaluate_factor(FactorEvaluator(), features, labels, status)
    assert report.report_metadata["title_prefix"].startswith("EXPLORATORY")


def test_fitted_transform_records_period_parameters_and_unsupported_neutralization() -> None:
    features, _ = evaluation_frames()
    scaler = RobustScaler(
        "robust-v1", "features-v1", date(2024, 1, 2), date(2024, 1, 3)
    )
    scaler.fit(features)
    transformed = scaler.transform(features)
    assert "transformed_value" in transformed.columns
    assert scaler.parameters()["dataset_version"] == "features-v1"
    with pytest.raises(UnsupportedNeutralizationError):
        sector_neutralize(features)


def test_cli_requires_pinned_versions_and_offers_offline_dry_run(tmp_path: Path) -> None:
    config = Path("config/default.yaml")
    with pytest.raises(SystemExit):
        main(["--config", str(config), "build-features", "--features", "momentum_5"])
    code = main(
        [
            "--config",
            str(config),
            "build-features",
            "--dataset-version",
            "prices-v1",
            "--universe-version",
            "universe-v1",
            "--calendar-version",
            "calendar-v1",
            "--universe-metadata-version",
            "universe-meta-v1",
            "--features",
            "momentum_5",
            "--feature-set-version",
            "baseline-v1",
            "--dry-run",
        ]
    )
    assert code == 0
