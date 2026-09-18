"""Leakage-safe baseline experiment orchestration and candidate model artifacts."""

from __future__ import annotations

import hashlib
import json
import platform
import statistics
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

import polars as pl

from island_quant.ml.dataset import SupervisedDataset
from island_quant.ml.evaluation import (
    PredictionLedger,
    block_bootstrap_daily_spearman,
    evaluate_predictions,
)
from island_quant.ml.models import (
    DummyRegressor,
    ElasticNetRegressor,
    LinearRegressor,
    Regressor,
    contract,
    fit_regressor,
)
from island_quant.ml.preprocessing import FoldPreprocessor, ImputationPolicy
from island_quant.ml.splitting import WalkForwardResult
from island_quant.ml.weighting import SampleWeightPolicy, sample_weights
from island_quant.research.completeness import ResearchCompleteness
from island_quant.storage.provenance import CodeProvenance, capture_code_provenance


class ModelStatus(StrEnum):
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    PAPER = "paper"
    LIVE = "live"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    version: str
    status: ModelStatus
    model_state: dict[str, Any]
    preprocessing: dict[str, Any]
    feature_names: tuple[str, ...]
    feature_dtypes: tuple[str, ...]
    hyperparameters: dict[str, Any]
    training_period: tuple[str, str]
    validation_period: tuple[str, str]
    test_period: tuple[str, str]
    dataset_version: str
    feature_artifact_version: str
    label_version: str
    split_manifest: dict[str, Any]
    validation_metrics: dict[str, Any]
    oos_metrics: dict[str, Any]
    random_seed: int
    runtime_versions: dict[str, str]
    code_provenance: dict[str, Any]
    completeness_status: str
    model_card: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    experiment_id: str
    prediction_ledger: PredictionLedger
    model_artifacts: tuple[ModelArtifact, ...]
    experiment_inventory: tuple[dict[str, Any], ...]
    aggregate_metrics: dict[str, Any]
    split_manifest: dict[str, Any]
    final_holdout_access_count: int


class CandidateModelRegistry:
    def __init__(self) -> None:
        self._artifacts: dict[str, ModelArtifact] = {}

    def add(self, artifact: ModelArtifact) -> None:
        if artifact.status is not ModelStatus.CANDIDATE:
            raise ValueError("Slice 4 registry accepts Candidate artifacts only")
        self._artifacts[artifact.version] = artifact

    def get(self, version: str) -> ModelArtifact:
        return self._artifacts[version]

    def promote(
        self,
        version: str,
        target: ModelStatus,
        *,
        dirty: bool,
        completeness: ResearchCompleteness,
        final_holdout_passed: bool,
    ) -> None:
        _ = self.get(version)
        if target is not ModelStatus.CANDIDATE:
            reasons = []
            if dirty:
                reasons.append("dirty_worktree")
            if completeness.classification != "validated":
                reasons.append("incomplete_point_in_time_data")
            if not final_holdout_passed:
                reasons.append("final_holdout_not_passed")
            reasons.append("slice4_candidate_only")
            raise RuntimeError(f"model promotion rejected: {','.join(reasons)}")


class BaselineExperimentRunner:
    def __init__(
        self,
        *,
        random_seed: int = 42,
        sample_weight_policy: SampleWeightPolicy = SampleWeightPolicy.EQUAL_DECISION_DATE,
        selection_metric: str = "validation_mean_daily_spearman_ic",
        provenance_provider: Callable[[], CodeProvenance] = capture_code_provenance,
    ) -> None:
        if selection_metric != "validation_mean_daily_spearman_ic":
            raise ValueError("Slice 4 selection metric is precommitted to validation rank IC")
        self.random_seed = random_seed
        self.sample_weight_policy = sample_weight_policy
        self.selection_metric = selection_metric
        self.provenance_provider = provenance_provider

    def run(
        self,
        supervised: SupervisedDataset,
        splits: WalkForwardResult,
        completeness: ResearchCompleteness,
        *,
        experiment_id: str,
        created_at: datetime,
        bootstrap_block_length: int = 5,
        bootstrap_resamples: int = 100,
    ) -> ExperimentResult:
        if created_at.tzinfo is None:
            raise ValueError("experiment created_at must be timezone-aware")
        ledger_rows: list[dict[str, Any]] = []
        artifacts: list[ModelArtifact] = []
        inventory: list[dict[str, Any]] = []
        provenance = self.provenance_provider()
        for fold in splits.folds:
            preprocessor = FoldPreprocessor(
                version="fold-preprocessor-v1",
                feature_schema=supervised.manifest.feature_schema,
                imputation_policy=ImputationPolicy.TRAIN_MEDIAN,
            )
            feature_names = supervised.manifest.feature_schema.names
            preprocessor.fit(fold.train, feature_names)
            train_x = preprocessor.transform(fold.train, feature_names)
            validation_x = preprocessor.transform(fold.validation, feature_names)
            test_x = preprocessor.transform(fold.test, feature_names)
            train_y = [float(value) for value in fold.train["label_value"].to_list()]
            validation_y = [
                float(value) for value in fold.validation["label_value"].to_list()
            ]
            weights = sample_weights(fold.train, self.sample_weight_policy)
            candidates = self._candidates(feature_names, supervised.manifest.target.kind)
            evaluated: list[tuple[float, tuple[Any, ...], Regressor, dict[str, Any]]] = []
            for estimator, simplicity, regularization in candidates:
                attempt: dict[str, Any] = {
                    "fold_id": fold.fold_id,
                    "model": estimator.contract.model_name,
                    "hyperparameters": estimator.contract.hyperparameters,
                    "status": "evaluated",
                }
                try:
                    fit_regressor(estimator, train_x, train_y, weights)
                    predictions = estimator.predict(validation_x)
                    validation_score = _mean_daily_rank_ic(
                        fold.validation, predictions, validation_y
                    )
                    attempt["validation_selection_metric"] = validation_score
                    score = validation_score if validation_score is not None else float("-inf")
                    tie_key = (
                        simplicity,
                        -regularization,
                        len(feature_names),
                        estimator.contract.model_name,
                        json.dumps(estimator.contract.hyperparameters, sort_keys=True),
                    )
                    evaluated.append((score, tie_key, estimator, attempt))
                except (ArithmeticError, RuntimeError, ValueError) as exc:
                    attempt["status"] = "rejected"
                    attempt["reason"] = type(exc).__name__
                inventory.append(attempt)
            if not evaluated:
                raise RuntimeError(f"no model candidate succeeded for {fold.fold_id}")
            selected_score, _, selected, selected_attempt = sorted(
                evaluated, key=lambda item: (-item[0], item[1])
            )[0]
            selected_attempt["selected"] = True
            validation_predictions = selected.predict(validation_x)
            test_predictions = selected.predict(test_x)
            model_version = _model_version(
                selected.state(),
                preprocessor.parameters(),
                fold.fold_id,
                supervised.manifest.checksum,
            )
            ledger_rows.extend(
                _prediction_rows(
                    fold.validation,
                    validation_predictions,
                    experiment_id,
                    fold.fold_id,
                    selected.contract.model_version,
                    "validation",
                    fold.fit_cutoff,
                    supervised.manifest.feature_artifact_version,
                    model_version,
                    created_at,
                )
            )
            ledger_rows.extend(
                _prediction_rows(
                    fold.test,
                    test_predictions,
                    experiment_id,
                    fold.fold_id,
                    selected.contract.model_version,
                    "test",
                    fold.fit_cutoff,
                    supervised.manifest.feature_artifact_version,
                    model_version,
                    created_at,
                )
            )
            validation_ledger = PredictionLedger.build(
                [
                    row
                    for row in ledger_rows
                    if row["fold_id"] == fold.fold_id
                    and row["split_role"] == "validation"
                ]
            )
            validation_metrics = _json_metrics(evaluate_predictions(validation_ledger))
            fold_test_ledger = PredictionLedger.build(
                [
                    row
                    for row in ledger_rows
                    if row["fold_id"] == fold.fold_id and row["split_role"] == "test"
                ]
            )
            oos_metrics = _json_metrics(evaluate_predictions(fold_test_ledger))
            model_card = _model_card(
                selected,
                supervised,
                fold,
                completeness,
                validation_metrics,
                oos_metrics,
                self.sample_weight_policy,
            )
            artifacts.append(
                ModelArtifact(
                    version=model_version,
                    status=ModelStatus.CANDIDATE,
                    model_state=selected.state(),
                    preprocessing=preprocessor.parameters(),
                    feature_names=feature_names,
                    feature_dtypes=supervised.manifest.feature_schema.dtypes,
                    hyperparameters=selected.contract.hyperparameters,
                    training_period=(str(fold.train_start), str(fold.train_end)),
                    validation_period=(str(fold.validation_start), str(fold.validation_end)),
                    test_period=(str(fold.test_start), str(fold.test_end)),
                    dataset_version=supervised.manifest.dataset_version,
                    feature_artifact_version=supervised.manifest.feature_artifact_version,
                    label_version=supervised.manifest.label_version,
                    split_manifest=asdict(splits.manifest),
                    validation_metrics=validation_metrics,
                    oos_metrics=oos_metrics,
                    random_seed=self.random_seed,
                    runtime_versions={
                        "python": platform.python_version(),
                        "polars": pl.__version__,
                        "implementation": "island_quant",
                    },
                    code_provenance=asdict(provenance),
                    completeness_status=completeness.classification,
                    model_card=model_card,
                )
            )
        ledger = PredictionLedger.build(ledger_rows)
        test_ledger = PredictionLedger.build(
            ledger.frame.filter(pl.col("split_role") == "test").to_dicts()
        )
        aggregate = _json_metrics(evaluate_predictions(test_ledger))
        aggregate["block_bootstrap_daily_spearman"] = asdict(
            block_bootstrap_daily_spearman(
                test_ledger,
                seed=self.random_seed,
                block_length=bootstrap_block_length,
                resample_count=bootstrap_resamples,
            )
        )
        aggregate["selection_metric"] = self.selection_metric
        aggregate["selection_uses_test"] = False
        aggregate["experiment_inventory_count"] = len(inventory)
        aggregate["folds"] = [
            {
                "fold_id": fold.fold_id,
                "train_range": [str(fold.train_start), str(fold.train_end)],
                "validation_range": [str(fold.validation_start), str(fold.validation_end)],
                "test_range": [str(fold.test_start), str(fold.test_end)],
                "purged_sample_count": fold.purged_sample_count,
                "embargoed_sample_count": fold.embargoed_sample_count,
            }
            for fold in splits.folds
        ]
        fold_rank_ic = [
            float(artifact.oos_metrics["mean_spearman_ic"])
            for artifact in artifacts
            if artifact.oos_metrics.get("mean_spearman_ic") is not None
        ]
        aggregate["fold_stability"] = {
            "fold_count": len(fold_rank_ic),
            "mean_test_spearman_ic": (
                statistics.fmean(fold_rank_ic) if fold_rank_ic else None
            ),
            "test_spearman_ic_standard_deviation": (
                statistics.stdev(fold_rank_ic) if len(fold_rank_ic) > 1 else None
            ),
        }
        return ExperimentResult(
            experiment_id,
            ledger,
            tuple(artifacts),
            tuple(inventory),
            aggregate,
            {**asdict(splits.manifest), "final_holdout_access_count": 0},
            0,
        )

    def _candidates(
        self, feature_names: tuple[str, ...], target: str
    ) -> list[tuple[Regressor, int, float]]:
        candidates: list[tuple[Regressor, int, float]] = []
        for strategy in ("mean", "zero"):
            specification = contract(
                f"dummy_{strategy}",
                "1.0.0",
                {"strategy": strategy},
                feature_names,
                target,
                random_seed=self.random_seed,
            )
            candidates.append((DummyRegressor(specification, strategy), 0, 0.0))
        linear_contract = contract(
            "linear_regression", "1.0.0", {}, feature_names, target, random_seed=self.random_seed
        )
        candidates.append((LinearRegressor(linear_contract), 1, 0.0))
        for alpha in (0.1, 1.0, 10.0):
            ridge_contract = contract(
                "ridge",
                "1.0.0",
                {"alpha": alpha},
                feature_names,
                target,
                random_seed=self.random_seed,
            )
            candidates.append((LinearRegressor(ridge_contract, alpha), 2, alpha))
        for alpha in (0.1, 1.0):
            for l1_ratio in (0.25, 0.5, 0.75):
                elastic_contract = contract(
                    "elastic_net",
                    "1.0.0",
                    {"alpha": alpha, "l1_ratio": l1_ratio},
                    feature_names,
                    target,
                    random_seed=self.random_seed,
                )
                candidates.append(
                    (
                        ElasticNetRegressor(elastic_contract, alpha, l1_ratio),
                        3,
                        alpha,
                    )
                )
        return candidates


def _mean_daily_rank_ic(
    frame: pl.DataFrame, predictions: list[float], targets: list[float]
) -> float | None:
    working = frame.with_columns(
        pl.Series("prediction", predictions), pl.Series("target_for_selection", targets)
    )
    values: list[float] = []
    for decision_time in working["decision_time"].unique().to_list():
        cross_section = working.filter(pl.col("decision_time") == decision_time)
        correlation = _rank_correlation(
            cross_section["prediction"].to_list(),
            cross_section["target_for_selection"].to_list(),
        )
        if correlation is not None:
            values.append(correlation)
    return sum(values) / len(values) if values else None


def _rank_correlation(left: list[float], right: list[float]) -> float | None:
    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda index: (values[index], index))
        result = [0.0] * len(values)
        cursor = 0
        while cursor < len(order):
            end = cursor + 1
            while end < len(order) and values[order[end]] == values[order[cursor]]:
                end += 1
            average_rank = (cursor + 1 + end) / 2
            for index in order[cursor:end]:
                result[index] = average_rank
            cursor = end
        return result

    left_ranks, right_ranks = ranks(left), ranks(right)
    if len(left) < 2:
        return None
    left_mean = sum(left_ranks) / len(left_ranks)
    right_mean = sum(right_ranks) / len(right_ranks)
    numerator = sum(
        (x - left_mean) * (y - right_mean)
        for x, y in zip(left_ranks, right_ranks, strict=True)
    )
    denominator = (
        sum((x - left_mean) ** 2 for x in left_ranks)
        * sum((y - right_mean) ** 2 for y in right_ranks)
    ) ** 0.5
    return numerator / denominator if denominator else None


def _prediction_rows(
    frame: pl.DataFrame,
    predictions: list[float],
    experiment_id: str,
    fold_id: str,
    model_version: str,
    split_role: str,
    fit_cutoff: datetime,
    feature_artifact_version: str,
    model_artifact_version: str,
    created_at: datetime,
) -> list[dict[str, Any]]:
    return [
        {
            "experiment_id": experiment_id,
            "fold_id": fold_id,
            "model_version": model_version,
            "instrument_id": row["instrument_id"],
            "decision_time": row["decision_time"],
            "prediction": prediction,
            "target": row["label_value"],
            "split_role": split_role,
            "fit_cutoff": fit_cutoff,
            "feature_artifact_version": feature_artifact_version,
            "model_artifact_version": model_artifact_version,
            "created_at": created_at,
        }
        for row, prediction in zip(frame.to_dicts(), predictions, strict=True)
    ]


def _model_version(
    model_state: dict[str, Any], preprocessing: dict[str, Any], fold_id: str, dataset_checksum: str
) -> str:
    payload = {
        "model": model_state,
        "preprocessing": preprocessing,
        "fold_id": fold_id,
        "dataset_checksum": dataset_checksum,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _json_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.to_dicts() if isinstance(value, pl.DataFrame) else value
        for key, value in metrics.items()
    }


def _model_card(
    estimator: Regressor,
    supervised: SupervisedDataset,
    fold: Any,
    completeness: ResearchCompleteness,
    validation_metrics: dict[str, Any],
    oos_metrics: dict[str, Any],
    weighting: SampleWeightPolicy,
) -> dict[str, Any]:
    return {
        "title": "EXPLORATORY — INCOMPLETE POINT-IN-TIME REFERENCE DATA",
        "intended_use": "offline Taiwan-equity factor research baseline",
        "not_intended_use": "live trading, investment advice, or production alpha claims",
        "dataset_version": supervised.manifest.dataset_version,
        "period": {
            "train": [str(fold.train_start), str(fold.train_end)],
            "validation": [str(fold.validation_start), str(fold.validation_end)],
            "test": [str(fold.test_start), str(fold.test_end)],
        },
        "universe_version": supervised.manifest.universe_version,
        "features": supervised.manifest.feature_schema.names,
        "target": asdict(supervised.manifest.target),
        "split_method": fold.fold_id,
        "purged_sample_count": fold.purged_sample_count,
        "embargoed_sample_count": fold.embargoed_sample_count,
        "preprocessing": "fold-local median, winsorization, standardization",
        "model": estimator.contract.model_name,
        "hyperparameters": estimator.contract.hyperparameters,
        "sample_weighting": weighting.value,
        "validation_metrics": validation_metrics,
        "oos_metrics": oos_metrics,
        "completeness_status": completeness.classification,
        "known_biases": ["incomplete PIT listing/suspension/corporate-action references"],
        "known_limitations": ["gross-before-costs", "small baseline grid"],
        "gross_before_costs_warning": True,
        "failure_conditions": ["schema drift", "PIT incompleteness", "data-quality failure"],
        "promotion_restrictions": "Slice 4 Candidate only; no Paper or Live promotion",
        "serialization_safety": (
            "JSON parameters only; no external pickle/joblib loading interface"
        ),
    }
