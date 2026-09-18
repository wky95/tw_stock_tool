from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from island_quant.cli import main
from island_quant.ml.artifacts import ExperimentStore
from island_quant.ml.dataset import (
    FeatureMatrixSchema,
    MissingFeaturePolicy,
    SupervisedDataset,
    SupervisedDatasetBuilder,
    SupervisedDatasetManifest,
    TargetContract,
)
from island_quant.ml.evaluation import (
    PredictionLedger,
    block_bootstrap_daily_spearman,
)
from island_quant.ml.experiment import (
    BaselineExperimentRunner,
    CandidateModelRegistry,
    ModelStatus,
)
from island_quant.ml.models import (
    DummyRegressor,
    ElasticNetRegressor,
    LinearRegressor,
    contract,
    fit_regressor,
)
from island_quant.ml.preprocessing import FoldPreprocessor, ImputationPolicy
from island_quant.ml.splitting import SplitMethod, WalkForwardConfig, WalkForwardSplitter
from island_quant.ml.weighting import SampleWeightPolicy, sample_weights
from island_quant.research.completeness import ResearchCompleteness
from island_quant.storage.provenance import CodeProvenance

TAIPEI = ZoneInfo("Asia/Taipei")


def sessions(count: int = 36) -> list[date]:
    result: list[date] = []
    cursor = date(2024, 1, 1)
    while len(result) < count:
        if cursor.weekday() < 5:
            result.append(cursor)
        cursor += timedelta(days=1)
    return result


def sample_frame(count: int = 36, symbols: int = 6) -> pl.DataFrame:
    rows = []
    days = sessions(count)
    for day_index, day in enumerate(days):
        for symbol_index in range(symbols):
            decision = datetime.combine(day, time(18), tzinfo=TAIPEI)
            interval_start = datetime.combine(
                day + timedelta(days=1), time(9), tzinfo=TAIPEI
            )
            rows.append(
                {
                    "instrument_id": f"{1000 + symbol_index}",
                    "decision_time": decision,
                    "decision_date": day,
                    "feature_vector": [
                        float(symbol_index + day_index / 10),
                        float((symbol_index % 3) - day_index / 20),
                    ],
                    "feature_set_version": "features-v1",
                    "feature_artifact_version": "feature-artifact-v1",
                    "label_value": float(symbol_index) / 100 + day_index / 1000,
                    "label_version": "label-v1",
                    "label_interval_start": interval_start,
                    "label_interval_end": interval_start + timedelta(days=1),
                    "universe_version": "universe-v1",
                    "dataset_version": "prices-v1",
                    "availability_policy_version": "availability-v1",
                    "market": "twse",
                    "valid": True,
                    "invalid_reason": None,
                }
            )
    return pl.DataFrame(rows, infer_schema_length=None)


def supervised(frame: pl.DataFrame | None = None) -> SupervisedDataset:
    actual = frame if frame is not None else sample_frame()
    manifest = SupervisedDatasetManifest(
        "supervised-v1",
        FeatureMatrixSchema(("f1", "f2"), ("float64", "float64")),
        "features-v1",
        "feature-artifact-v1",
        "label-v1",
        "label-artifact-v1",
        "universe-v1",
        "prices-v1",
        "availability-v1",
        TargetContract(
            "raw_forward_return",
            "label-v1",
            1,
            "next_session_open",
            "following_session_open",
            "gross_before_costs",
        ),
        MissingFeaturePolicy.INVALID,
        tuple((str(day), 6) for day in sessions()),
        tuple((str(day), 6) for day in sessions()),
        "dataset-checksum-v1",
    )
    return SupervisedDataset(actual, manifest)


def split(frame: pl.DataFrame | None = None, method: SplitMethod = SplitMethod.EXPANDING):
    return WalkForwardSplitter(
        WalkForwardConfig("split-v1", method, 12, 4, 4, 8, 1, 3), sessions()
    ).split(frame if frame is not None else sample_frame())


def completeness(complete: bool = False) -> ResearchCompleteness:
    missing = 0 if complete else 1
    return ResearchCompleteness(
        complete,
        missing,
        missing,
        missing,
        missing,
        "prices-v1",
        "features-v1",
        "label-v1",
        "availability-v1",
    )


def ledger(days: int = 8) -> PredictionLedger:
    rows = []
    for day_index, day in enumerate(sessions(days)):
        for symbol_index in range(5):
            rows.append(
                {
                    "experiment_id": "experiment-v1",
                    "fold_id": "fold-001",
                    "model_version": "model-v1",
                    "instrument_id": str(symbol_index),
                    "decision_time": datetime.combine(day, time(18), tzinfo=TAIPEI),
                    "prediction": float(symbol_index),
                    "target": float(symbol_index + (day_index % 2) / 10),
                    "split_role": "test",
                    "fit_cutoff": datetime(2023, 12, 31, tzinfo=TAIPEI),
                    "feature_artifact_version": "features-v1",
                    "model_artifact_version": "model-artifact-v1",
                    "created_at": datetime(2024, 3, 1, tzinfo=UTC),
                }
            )
    return PredictionLedger.build(rows)


def test_random_split_is_rejected_and_walk_forward_chronology_holds() -> None:
    with pytest.raises(ValueError):
        SplitMethod("random_shuffle")
    result = split()
    assert result.folds
    for fold in result.folds:
        assert fold.train_end < fold.validation_start < fold.test_start


def test_rolling_train_window_is_fixed() -> None:
    result = split(method=SplitMethod.ROLLING)
    assert {fold.train["decision_date"].n_unique() for fold in result.folds} == {12}


def test_actual_label_interval_overlap_is_purged() -> None:
    frame = sample_frame()
    validation_start = sessions()[13]
    overlapping = frame.with_columns(
        pl.when(pl.col("decision_date") == sessions()[0])
        .then(
            pl.lit(
                datetime.combine(
                    validation_start + timedelta(days=3), time(9), tzinfo=TAIPEI
                )
            )
        )
        .otherwise(pl.col("label_interval_end"))
        .alias("label_interval_end")
    )
    first = split(overlapping).folds[0]
    assert first.purged_sample_count == 6
    assert sessions()[0] not in first.train["decision_date"].unique().to_list()


def test_embargo_counts_trading_sessions_across_weekend() -> None:
    result = split()
    first = result.folds[0]
    expected_validation = sessions()[13]
    assert first.validation_start == expected_validation
    assert first.embargoed_sample_count == 6
    assert (expected_validation - first.train_end).days >= 1


def test_final_holdout_is_not_used_by_folds_or_tuning() -> None:
    result = split()
    holdout_dates = set(result.manifest.final_holdout_dates)
    used = {
        day
        for fold in result.folds
        for table in (fold.train, fold.validation, fold.test)
        for day in table["decision_date"].unique().to_list()
    }
    assert holdout_dates.isdisjoint(used)
    assert result.final_holdout.height == 18


def test_preprocessing_is_fit_only_on_training_and_future_changes_do_not_affect_it() -> None:
    fold = split().folds[0]
    schema = FeatureMatrixSchema(("f1", "f2"), ("float64", "float64"))
    first = FoldPreprocessor("v1", schema, ImputationPolicy.TRAIN_MEDIAN)
    first.fit(fold.train, schema.names)
    changed_validation = fold.validation.with_columns(
        pl.col("feature_vector").list.eval(pl.element() * 1_000_000)
    )
    second = FoldPreprocessor("v1", schema, ImputationPolicy.TRAIN_MEDIAN)
    second.fit(fold.train, schema.names)
    second.transform(changed_validation, schema.names)
    assert first.parameters() == second.parameters()


def test_feature_order_schema_check_rejects_reordering() -> None:
    processor = FoldPreprocessor(
        "v1",
        FeatureMatrixSchema(("f1", "f2"), ("float64", "float64")),
        ImputationPolicy.TRAIN_MEDIAN,
    )
    with pytest.raises(ValueError, match="order mismatch"):
        processor.fit(sample_frame().head(12), ("f2", "f1"))


@pytest.mark.parametrize("forbidden", ["label_value", "market"])
def test_dataset_builder_rejects_label_and_metadata_features(forbidden: str) -> None:
    features, labels, universe = builder_inputs()
    with pytest.raises(ValueError):
        build_dataset(features, labels, universe, (forbidden,))


def test_dataset_builder_exact_join_determinism_and_duplicate_rejection() -> None:
    features, labels, universe = builder_inputs()
    first = build_dataset(features, labels, universe, ("f1", "f2"))
    second = build_dataset(features.reverse(), labels.reverse(), universe, ("f1", "f2"))
    assert first.manifest.checksum == second.manifest.checksum
    assert first.frame.to_dicts() == second.frame.to_dicts()
    with pytest.raises(ValueError, match="duplicate"):
        build_dataset(features.vstack(features.head(1)), labels, universe, ("f1", "f2"))


def test_prediction_ledger_rejects_duplicate_and_in_sample_rows() -> None:
    rows = ledger(1).frame.to_dicts()
    assert all(row["decision_time"].tzinfo is not None for row in rows)
    with pytest.raises(ValueError, match="duplicate"):
        PredictionLedger.build([rows[0], rows[0]])
    naive = dict(rows[0], created_at=datetime(2024, 3, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        PredictionLedger.build([naive])
    rows[0]["split_role"] = "train"
    with pytest.raises(ValueError, match="in-sample"):
        PredictionLedger.build(rows)


def test_dummy_baselines_are_hand_reproducible() -> None:
    mean = DummyRegressor(contract("dummy", "v1", {}, ("f",), "raw"), "mean")
    mean.fit([[1], [2]], [2, 8], [3, 1])
    assert mean.predict([[10]]) == [3.5]
    zero = DummyRegressor(contract("dummy", "v1", {}, ("f",), "raw"), "zero")
    zero.fit([[1]], [99], [1])
    assert zero.predict([[10]]) == [0.0]


def test_ridge_and_elastic_net_are_deterministic() -> None:
    x = [[0.0, 1.0], [1.0, 0.0], [2.0, 1.0], [3.0, 2.0]]
    y = [0.5, 1.0, 2.5, 4.0]
    weights = [1.0] * 4
    ridge_states = []
    elastic_states = []
    for _ in range(2):
        ridge = LinearRegressor(contract("ridge", "v1", {}, ("a", "b"), "raw"), 1.0)
        ridge.fit(x, y, weights)
        ridge_states.append(ridge.state())
        elastic = ElasticNetRegressor(
            contract("elastic", "v1", {}, ("a", "b"), "raw"), 0.1, 0.5
        )
        elastic.fit(x, y, weights)
        elastic_states.append(elastic.state())
    assert ridge_states[0] == ridge_states[1]
    assert elastic_states[0] == elastic_states[1]


def test_equal_date_weighting_has_equal_daily_total() -> None:
    frame = sample_frame(2).filter(
        ~((pl.col("decision_date") == sessions(2)[1]) & (pl.col("instrument_id") == "1005"))
    )
    weights = sample_weights(frame, SampleWeightPolicy.EQUAL_DECISION_DATE)
    weighted = frame.with_columns(pl.Series("weight", weights))
    daily_totals = weighted.group_by("decision_date").agg(pl.col("weight").sum())
    assert daily_totals["weight"].to_list() == pytest.approx([1, 1])


def test_unsupported_sample_weight_is_explicit() -> None:
    model = DummyRegressor(
        contract("dummy", "v1", {}, ("f",), "raw", supports_sample_weight=False), "zero"
    )
    with pytest.raises(ValueError, match="does not support"):
        fit_regressor(model, [[1.0]], [1.0], [1.0])


def test_dirty_and_incomplete_artifacts_cannot_be_validated() -> None:
    result = run_experiment()
    artifact = result.model_artifacts[0]
    registry = CandidateModelRegistry()
    registry.add(artifact)
    with pytest.raises(RuntimeError, match="dirty_worktree"):
        registry.promote(
            artifact.version,
            ModelStatus.VALIDATED,
            dirty=True,
            completeness=completeness(True),
            final_holdout_passed=True,
        )
    with pytest.raises(RuntimeError, match="incomplete_point_in_time_data"):
        registry.promote(
            artifact.version,
            ModelStatus.VALIDATED,
            dirty=False,
            completeness=completeness(False),
            final_holdout_passed=True,
        )


def test_block_bootstrap_is_reproducible_and_small_sample_is_unavailable() -> None:
    first = block_bootstrap_daily_spearman(ledger(), seed=7, block_length=2, resample_count=20)
    second = block_bootstrap_daily_spearman(ledger(), seed=7, block_length=2, resample_count=20)
    assert first == second
    small = block_bootstrap_daily_spearman(ledger(2), seed=7, block_length=2, resample_count=20)
    assert small.available is False
    assert small.unavailable_reason == "insufficient_decision_date_blocks"


def test_future_label_changes_do_not_change_earlier_fold_model_or_selection() -> None:
    baseline = run_experiment()
    cutoff = split().folds[0].test_end
    changed = sample_frame().with_columns(
        pl.when(pl.col("decision_date") > cutoff)
        .then(pl.col("label_value") * -10_000)
        .otherwise(pl.col("label_value"))
        .alias("label_value")
    )
    modified = run_experiment(changed)
    assert baseline.model_artifacts[0].model_state == modified.model_artifacts[0].model_state
    assert (
        baseline.model_artifacts[0].hyperparameters
        == modified.model_artifacts[0].hyperparameters
    )


def test_test_performance_is_never_used_for_selection() -> None:
    result = run_experiment()
    first_test_dates = split().folds[0].test["decision_date"].unique().to_list()
    changed = sample_frame().with_columns(
        pl.when(pl.col("decision_date").is_in(first_test_dates))
        .then(pl.col("label_value") * -100_000)
        .otherwise(pl.col("label_value"))
        .alias("label_value")
    )
    changed_result = run_experiment(changed)
    assert result.aggregate_metrics["selection_uses_test"] is False
    selected = [item for item in result.experiment_inventory if item.get("selected")]
    assert selected
    assert all("test" not in key for item in selected for key in item)
    assert result.model_artifacts[0].hyperparameters == (
        changed_result.model_artifacts[0].hyperparameters
    )


def test_candidate_artifact_card_provenance_and_json_store(tmp_path: Path) -> None:
    result = run_experiment()
    artifact = result.model_artifacts[0]
    assert artifact.status is ModelStatus.CANDIDATE
    assert artifact.code_provenance["git_commit"] == "commit-v1"
    assert artifact.model_card["title"] == (
        "EXPLORATORY — INCOMPLETE POINT-IN-TIME REFERENCE DATA"
    )
    checksum, path = ExperimentStore(tmp_path).save(result)
    assert ExperimentStore(tmp_path).inspect(checksum)["experiment_id"] == "experiment-v1"
    assert path.suffix == ".json"
    assert not list(tmp_path.rglob("*.pkl"))


def test_ml_cli_requires_versions_and_supports_offline_dry_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "--config",
            "config/default.yaml",
            "run-ml-experiment",
            "--experiment-id",
            "fixture-v1",
            "--supervised-version",
            "supervised-v1",
            "--supervised-artifact-version",
            "artifact-v1",
            "--calendar-version",
            "calendar-v1",
            "--universe-metadata-version",
            "metadata-v1",
            "--split-method",
            "expanding_walk_forward",
            "--split-version",
            "split-v1",
            "--train-sessions",
            "12",
            "--validation-sessions",
            "4",
            "--test-sessions",
            "4",
            "--step-sessions",
            "8",
            "--dry-run",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def run_experiment(frame: pl.DataFrame | None = None):
    dataset = supervised(frame)
    return BaselineExperimentRunner(
        provenance_provider=lambda: CodeProvenance("commit-v1", False, "tree-v1")
    ).run(
        dataset,
        split(dataset.frame),
        completeness(False),
        experiment_id="experiment-v1",
        created_at=datetime(2024, 3, 1, tzinfo=UTC),
        bootstrap_block_length=2,
        bootstrap_resamples=20,
    )


def builder_inputs() -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    feature_rows = []
    label_rows = []
    universe_rows = []
    for day_index, day in enumerate(sessions(3)):
        decision = datetime.combine(day, time(18), tzinfo=TAIPEI)
        for symbol_index in range(2):
            instrument = str(1000 + symbol_index)
            for feature_index, feature_name in enumerate(("f1", "f2")):
                feature_rows.append(
                    {
                        "instrument_id": instrument,
                        "decision_time": decision,
                        "feature_name": feature_name,
                        "value": float(day_index + symbol_index + feature_index),
                        "valid": True,
                    }
                )
            start = datetime.combine(day + timedelta(days=1), time(9), tzinfo=TAIPEI)
            label_rows.append(
                {
                    "instrument_id": instrument,
                    "decision_time": decision,
                    "market": "twse",
                    "label_value": float(symbol_index),
                    "label_interval_start": start,
                    "label_interval_end": start + timedelta(hours=4),
                    "valid": True,
                    "invalid_reason": None,
                }
            )
            universe_rows.append(
                {"instrument_id": instrument, "trade_date": day, "eligible": True}
            )
    return (
        pl.DataFrame(feature_rows, infer_schema_length=None),
        pl.DataFrame(label_rows, infer_schema_length=None),
        pl.DataFrame(universe_rows, infer_schema_length=None),
    )


def build_dataset(
    features: pl.DataFrame,
    labels: pl.DataFrame,
    universe: pl.DataFrame,
    feature_names: tuple[str, ...],
) -> SupervisedDataset:
    return SupervisedDatasetBuilder().build(
        features,
        labels,
        universe,
        version="supervised-v1",
        feature_names=feature_names,
        feature_set_version="features-v1",
        feature_artifact_version="feature-artifact-v1",
        label_artifact_version="label-artifact-v1",
        universe_version="universe-v1",
        dataset_version="prices-v1",
        availability_policy_version="availability-v1",
        target=TargetContract(
            "raw_forward_return",
            "label-v1",
            1,
            "next_open",
            "following_open",
            "gross_before_costs",
        ),
        missing_feature_policy=MissingFeaturePolicy.INVALID,
    )
