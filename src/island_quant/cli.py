"""Phase 0 operational commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import polars as pl
import yaml
from pydantic import ValidationError

from island_quant.config import AppSettings, load_settings
from island_quant.data.adapters.finmind import FinMindProvider
from island_quant.data.adapters.fixture import FixtureProvider
from island_quant.data.availability import AvailabilityPolicy
from island_quant.data.calendar import CanonicalTradingCalendar
from island_quant.data.ingestion import IngestionService
from island_quant.data.ports import HistoricalDataProvider
from island_quant.data.universe import (
    INCOMPLETE_UNIVERSE_WARNING,
    UniversePolicy,
    require_research_complete,
)
from island_quant.data.validation import DataValidationError, validate_daily_prices
from island_quant.features.baseline import BASELINE_FEATURE_VERSION, baseline_registry
from island_quant.features.engine import FeatureEngine
from island_quant.features.materialization import FeatureMaterializer
from island_quant.logging import configure_logging
from island_quant.ml.artifacts import (
    ExperimentStore,
    read_supervised_manifest,
    write_supervised_manifest,
)
from island_quant.ml.dataset import (
    MissingFeaturePolicy,
    SupervisedDataset,
    SupervisedDatasetBuilder,
    TargetContract,
    manifest_from_dict,
)
from island_quant.ml.experiment import BaselineExperimentRunner
from island_quant.ml.splitting import SplitMethod, WalkForwardConfig, WalkForwardSplitter
from island_quant.ml.weighting import SampleWeightPolicy
from island_quant.research.completeness import ResearchCompleteness
from island_quant.research.factor_evaluation import FactorEvaluator
from island_quant.storage.local import LocalArtifactStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="island-quant")
    parser.add_argument("--config", type=Path, default=Path("config/default.yaml"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-config", help="validate configuration and safety invariants")
    subparsers.add_parser("show-config", help="print effective non-secret configuration")
    subparsers.add_parser("doctor", help="check the Phase 0 installation")
    ingest = subparsers.add_parser(
        "ingest-data", help="ingest point-in-time daily price foundation"
    )
    ingest.add_argument("--provider", choices=("finmind", "fixture"))
    ingest.add_argument("--fixture-dir", type=Path)
    ingest.add_argument("--start", required=True)
    ingest.add_argument("--end", required=True)
    ingest.add_argument("--symbols", help="comma-separated symbols; omit for provider universe")
    ingest.add_argument("--dry-run", action="store_true")
    validate = subparsers.add_parser("validate-data", help="validate latest normalized datasets")
    validate.add_argument("--fail-on-warning", action="store_true")
    validate.add_argument("--research-override", action="store_true")
    build_features = subparsers.add_parser("build-features")
    _add_pinned_feature_arguments(build_features)
    build_features.add_argument("--features", required=True)
    build_features.add_argument("--feature-set-version", required=True)
    build_features.add_argument("--start")
    build_features.add_argument("--end")
    build_features.add_argument("--symbols")
    build_features.add_argument("--dry-run", action="store_true")
    inspect = subparsers.add_parser("inspect-feature")
    inspect.add_argument("--feature-set-version", required=True)
    inspect.add_argument("--feature-dataset-version", required=True)
    inspect.add_argument("--instrument", required=True)
    inspect.add_argument("--date", required=True)
    inspect.add_argument("--feature", required=True)
    evaluate = subparsers.add_parser("evaluate-factor")
    _add_evaluation_arguments(evaluate)
    report = subparsers.add_parser("generate-factor-report")
    _add_evaluation_arguments(report)
    report.add_argument("--output", type=Path, required=True)
    ml_dataset = subparsers.add_parser("build-ml-dataset")
    _add_ml_dataset_arguments(ml_dataset)
    experiment = subparsers.add_parser("run-ml-experiment")
    _add_ml_experiment_arguments(experiment)
    inspect_experiment = subparsers.add_parser("inspect-experiment")
    inspect_experiment.add_argument("--experiment-artifact-version", required=True)
    card = subparsers.add_parser("generate-model-card")
    card.add_argument("--experiment-artifact-version", required=True)
    card.add_argument("--output", type=Path, required=True)
    pipeline = subparsers.add_parser("run-research-pipeline")
    pipeline.add_argument("--pipeline-config", type=Path, required=True)
    pipeline.add_argument("--dry-run", action="store_true")
    pipeline.add_argument("--confirm-large-run", action="store_true")
    inspect_pipeline = subparsers.add_parser("inspect-pipeline-run")
    inspect_pipeline.add_argument("--run-version", required=True)
    run_backtest = subparsers.add_parser("run-backtest")
    run_backtest.add_argument("--demo", action="store_true")
    run_backtest.add_argument("--dry-run", action="store_true")
    for name in (
        "prediction-artifact-version",
        "model-artifact-version",
        "dataset-version",
        "universe-version",
        "calendar-version",
        "corporate-action-version",
        "benchmark-version",
        "target-policy-version",
    ):
        run_backtest.add_argument(f"--{name}")
    inspect_backtest = subparsers.add_parser("inspect-backtest")
    inspect_backtest.add_argument("--artifact-version", required=True)
    inspect_backtest.add_argument(
        "--artifact-namespace", choices=("candidate", "demo"), default="candidate"
    )
    compare_backtests = subparsers.add_parser("compare-backtests")
    compare_backtests.add_argument("--left-version", required=True)
    compare_backtests.add_argument("--right-version", required=True)
    compare_backtests.add_argument(
        "--artifact-namespace", choices=("candidate", "demo"), default="candidate"
    )
    backtest_report = subparsers.add_parser("generate-backtest-report")
    backtest_report.add_argument("--artifact-version", required=True)
    backtest_report.add_argument("--output", type=Path, required=True)
    backtest_report.add_argument(
        "--artifact-namespace", choices=("candidate", "demo"), default="candidate"
    )
    backtest_report.add_argument("--force", action="store_true")
    dashboard = subparsers.add_parser("dashboard", help="run the read-only research dashboard")
    dashboard.add_argument("--demo", action="store_true", help="use deterministic synthetic data")
    dashboard.add_argument("--artifact-version")
    dashboard.add_argument("--pipeline-run-version")
    dashboard.add_argument(
        "--artifact-namespace", choices=("candidate", "demo"), default="candidate"
    )
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--reload", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(settings.logging)
    if args.command == "validate-config":
        print(f"valid: {args.config}")
    elif args.command == "show-config":
        print(json.dumps(settings.safe_dump(), ensure_ascii=False, indent=2))
    elif args.command == "doctor":
        result = {"package": "island-quant", "version": version("island-quant"), "status": "ok"}
        print(json.dumps(result))
    elif args.command == "ingest-data":
        return _ingest_data(args, settings)
    elif args.command == "validate-data":
        return _validate_data(args, settings)
    elif args.command == "build-features":
        return _build_features(args, settings)
    elif args.command == "inspect-feature":
        return _inspect_feature(args, settings)
    elif args.command in {"evaluate-factor", "generate-factor-report"}:
        return _evaluate_factor(args, settings)
    elif args.command == "build-ml-dataset":
        return _build_ml_dataset(args, settings)
    elif args.command == "run-ml-experiment":
        return _run_ml_experiment(args, settings)
    elif args.command == "inspect-experiment":
        return _inspect_experiment(args, settings)
    elif args.command == "generate-model-card":
        return _generate_model_card(args, settings)
    elif args.command == "run-research-pipeline":
        return _run_research_pipeline(args)
    elif args.command == "inspect-pipeline-run":
        return _inspect_pipeline_run(args, settings)
    elif args.command == "run-backtest":
        return _run_backtest(args, settings)
    elif args.command == "inspect-backtest":
        return _inspect_backtest(args, settings)
    elif args.command == "compare-backtests":
        return _compare_backtests(args, settings)
    elif args.command == "generate-backtest-report":
        return _generate_backtest_report(args, settings)
    elif args.command == "dashboard":
        return _dashboard(args, settings)
    return 0


def _add_pinned_feature_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--universe-version", required=True)
    parser.add_argument("--calendar-version", required=True)
    parser.add_argument("--universe-metadata-version", required=True)


def _add_evaluation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--feature-set-version", required=True)
    parser.add_argument("--feature-dataset-version", required=True)
    parser.add_argument("--label-dataset-version", required=True)
    parser.add_argument("--label-version", required=True)
    parser.add_argument("--label-kind", required=True)
    parser.add_argument("--universe-metadata-version", required=True)
    parser.add_argument("--feature", required=True)
    parser.add_argument("--direction", choices=("long_high", "long_low"), default="long_high")
    parser.add_argument("--dry-run", action="store_true")


def _add_ml_dataset_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--supervised-version", required=True)
    parser.add_argument("--feature-set-version", required=True)
    parser.add_argument("--feature-artifact-version", required=True)
    parser.add_argument("--label-artifact-version", required=True)
    parser.add_argument("--label-version", required=True)
    parser.add_argument("--universe-version", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--availability-policy-version", required=True)
    parser.add_argument("--features", required=True)
    parser.add_argument(
        "--target-kind",
        required=True,
        choices=(
            "raw_forward_return",
            "benchmark_relative_forward_return",
            "cross_sectional_rank",
        ),
    )
    parser.add_argument("--return-horizon", type=int, required=True)
    parser.add_argument("--entry-definition", required=True)
    parser.add_argument("--exit-definition", required=True)
    parser.add_argument("--benchmark-definition")
    parser.add_argument("--rank-direction")
    parser.add_argument(
        "--missing-feature-policy",
        choices=tuple(item.value for item in MissingFeaturePolicy),
        default=MissingFeaturePolicy.INVALID.value,
    )
    parser.add_argument("--dry-run", action="store_true")


def _add_ml_experiment_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--supervised-version", required=True)
    parser.add_argument("--supervised-artifact-version", required=True)
    parser.add_argument("--calendar-version", required=True)
    parser.add_argument("--universe-metadata-version", required=True)
    parser.add_argument(
        "--split-method", choices=tuple(item.value for item in SplitMethod), required=True
    )
    parser.add_argument("--split-version", required=True)
    parser.add_argument("--train-sessions", type=int, required=True)
    parser.add_argument("--validation-sessions", type=int, required=True)
    parser.add_argument("--test-sessions", type=int, required=True)
    parser.add_argument("--step-sessions", type=int, required=True)
    parser.add_argument("--embargo-sessions", type=int, default=0)
    parser.add_argument("--final-holdout-sessions", type=int, default=0)
    parser.add_argument(
        "--sample-weight-policy",
        choices=tuple(item.value for item in SampleWeightPolicy),
        default=SampleWeightPolicy.EQUAL_DECISION_DATE.value,
    )
    parser.add_argument("--bootstrap-block-length", type=int, default=5)
    parser.add_argument("--bootstrap-resamples", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")


def _ingest_data(args: argparse.Namespace, settings: AppSettings) -> int:
    provider_name = args.provider or settings.data.provider
    symbols = (
        [item.strip() for item in args.symbols.split(",") if item.strip()] if args.symbols else None
    )
    plan = {
        "provider": provider_name,
        "start": args.start,
        "end": args.end,
        "symbols": symbols or "provider-universe",
        "raw_root": str(settings.data.raw_root),
        "normalized_root": str(settings.data.normalized_root),
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, "plan": plan}, ensure_ascii=False))
        return 0
    if provider_name == "fixture":
        if args.fixture_dir is None:
            print(
                "configuration error: --fixture-dir is required for fixture provider",
                file=sys.stderr,
            )
            return 2
        provider: HistoricalDataProvider = FixtureProvider(args.fixture_dir)
    else:
        provider = FinMindProvider(
            token=os.getenv("FINMIND_TOKEN"),
            attempts=settings.data.request_attempts,
            timeout_seconds=settings.data.request_timeout_seconds,
        )
    store = LocalArtifactStore(
        settings.data.raw_root,
        settings.data.normalized_root,
        settings.data.checkpoint_root,
        settings.data.catalog_path,
    )
    universe = settings.universe
    policy = UniversePolicy(
        version=universe.policy_version,
        minimum_listing_days=universe.minimum_listing_days,
        trailing_median_window=universe.trailing_median_window,
        minimum_trailing_median_traded_value=universe.minimum_trailing_median_traded_value,
        minimum_lookback_observations=universe.minimum_lookback_observations,
    )
    try:
        result = IngestionService(provider, store, policy, settings.data.schema_version).run(
            args.start, args.end, symbols
        )
    except DataValidationError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "quality_errors": exc.report.error_count,
                    "quality_warnings": exc.report.warning_count,
                }
            ),
            file=sys.stderr,
        )
        return 3
    except (OSError, RuntimeError, ValueError):
        print(
            json.dumps({"status": "failed", "error": "ingestion_failed"}),
            file=sys.stderr,
        )
        return 4
    print(
        json.dumps(
            {
                "status": "ok",
                "job_id": result.job_id,
                "quality_errors": result.quality_report.error_count,
                "quality_warnings": result.quality_report.warning_count,
                "artifacts": {
                    name: {"checksum": item.checksum, "rows": item.row_count}
                    for name, item in result.artifacts.items()
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _validate_data(args: argparse.Namespace, settings: AppSettings) -> int:
    store = LocalArtifactStore(
        settings.data.raw_root,
        settings.data.normalized_root,
        settings.data.checkpoint_root,
        settings.data.catalog_path,
    )
    prices = store.read_latest_dataset("daily_prices")
    instruments = store.read_latest_dataset("instrument_master")
    calendar = store.read_latest_dataset("trading_calendar")
    universe_metadata = store.read_latest_dataset("universe_metadata")
    if prices is None or instruments is None or calendar is None or universe_metadata is None:
        print(json.dumps({"status": "failed", "error": "normalized_data_missing"}))
        return 4
    report = validate_daily_prices(prices, instruments, calendar)
    incomplete = not bool(universe_metadata.row(0, named=True)["is_research_complete"])
    try:
        require_research_complete(universe_metadata, allow_incomplete=args.research_override)
    except RuntimeError:
        print(INCOMPLETE_UNIVERSE_WARNING, file=sys.stderr)
        return 6
    if incomplete:
        print(INCOMPLETE_UNIVERSE_WARNING, file=sys.stderr)
    print(
        json.dumps(
            {
                "status": "ok" if report.error_count == 0 else "failed",
                "errors": report.error_count,
                "warnings": report.warning_count,
                "issue_codes": sorted({issue.code for issue in report.issues}),
            }
        )
    )
    if report.error_count:
        return 3
    if args.fail_on_warning and report.warning_count:
        return 5
    return 0


def _store(settings: AppSettings) -> LocalArtifactStore:
    return LocalArtifactStore(
        settings.data.raw_root,
        settings.data.normalized_root,
        settings.data.checkpoint_root,
        settings.data.catalog_path,
    )


def _availability(settings: AppSettings) -> AvailabilityPolicy:
    return AvailabilityPolicy(
        version=settings.availability.policy_version,
        publication_time=settings.availability.publication_time,
        finalization_buffer=settings.availability.finalization_buffer,
    )


def _build_features(args: argparse.Namespace, settings: AppSettings) -> int:
    requested = tuple(item.strip() for item in args.features.split(",") if item.strip())
    plan = {
        "dataset_version": args.dataset_version,
        "universe_version": args.universe_version,
        "calendar_version": args.calendar_version,
        "universe_metadata_version": args.universe_metadata_version,
        "feature_set_version": args.feature_set_version,
        "features": requested,
        "start": args.start,
        "end": args.end,
        "symbols": args.symbols,
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, "plan": plan}, sort_keys=True))
        return 0
    store = _store(settings)
    prices = store.read_dataset_version("canonical_daily_prices", args.dataset_version)
    universe = store.read_dataset_version("universe_membership", args.universe_version)
    calendar_frame = store.read_dataset_version("trading_calendar", args.calendar_version)
    metadata_frame = store.read_dataset_version("universe_metadata", args.universe_metadata_version)
    prices, universe = _filter_feature_inputs(prices, universe, args)
    calendar = _canonical_calendar(calendar_frame)
    registry = baseline_registry(args.dataset_version)
    selected = tuple(registry.get(name, BASELINE_FEATURE_VERSION) for name in requested)
    feature_set = registry.build_feature_set(
        args.feature_set_version,
        tuple((item.name, item.version) for item in selected),
        args.dataset_version,
    )
    result = FeatureEngine(_availability(settings)).materialize(
        prices,
        universe,
        calendar,
        selected,
        feature_set,
        input_dataset_version=args.dataset_version,
        universe_version=args.universe_version,
    )
    completeness = _completeness(
        metadata_frame,
        dataset_version=args.dataset_version,
        feature_set_version=args.feature_set_version,
        label_version="not_applicable",
        availability_version=settings.availability.policy_version,
    )
    candidate = FeatureMaterializer(store).stage(
        result.frame,
        feature_set_version=args.feature_set_version,
        input_dataset_version=args.dataset_version,
        universe_version=args.universe_version,
        label_version="not_applicable_feature_build",
        availability_policy_version=settings.availability.policy_version,
        completeness=completeness,
        feature_set_checksum=feature_set.checksum,
    )
    print(
        json.dumps(
            {
                "status": "candidate",
                "artifact_path": str(candidate.artifact.parquet_path),
                "version": candidate.artifact.checksum,
                "checksum": candidate.artifact.checksum,
                "rows": candidate.artifact.row_count,
                "completeness_status": completeness.classification,
            },
            sort_keys=True,
        )
    )
    return 0


def _filter_feature_inputs(
    prices: pl.DataFrame, universe: pl.DataFrame, args: argparse.Namespace
) -> tuple[pl.DataFrame, pl.DataFrame]:
    if args.start:
        start = pl.lit(args.start).str.to_date()
        prices = prices.filter(pl.col("trade_date") >= start)
        universe = universe.filter(pl.col("trade_date") >= start)
    if args.end:
        end = pl.lit(args.end).str.to_date()
        prices = prices.filter(pl.col("trade_date") <= end)
        universe = universe.filter(pl.col("trade_date") <= end)
    if args.symbols:
        symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
        prices = prices.filter(pl.col("instrument_id").is_in(symbols))
        universe = universe.filter(pl.col("instrument_id").is_in(symbols))
    return prices, universe


def _canonical_calendar(frame: pl.DataFrame) -> CanonicalTradingCalendar:
    if {"market", "status", "timezone"}.issubset(frame.columns):
        return CanonicalTradingCalendar(frame, "pinned")
    days = sorted(frame["trade_date"].unique().to_list())
    if not days:
        raise ValueError("calendar snapshot is empty")
    return CanonicalTradingCalendar.from_trading_dates(
        days, days[0], days[-1], "pinned", market="tw"
    )


def _completeness(
    metadata: pl.DataFrame,
    *,
    dataset_version: str,
    feature_set_version: str,
    label_version: str,
    availability_version: str,
) -> ResearchCompleteness:
    row = metadata.row(0, named=True)
    return ResearchCompleteness(
        universe_complete=bool(row["is_research_complete"]),
        unknown_market_count=int(row["unknown_market_count"]),
        provisional_listing_date_count=int(row["provisional_listing_date_count"]),
        unsupported_corporate_action_count=int(row.get("unsupported_corporate_action_count", 1)),
        missing_suspension_status_count=int(row.get("missing_suspension_status_count", 1)),
        dataset_version=dataset_version,
        feature_set_version=feature_set_version,
        label_version=label_version,
        availability_policy_version=availability_version,
    )


def _inspect_feature(args: argparse.Namespace, settings: AppSettings) -> int:
    frame = _store(settings).read_dataset_version(
        f"features__{args.feature_set_version}", args.feature_dataset_version
    )
    selected = frame.filter(
        (pl.col("instrument_id") == args.instrument)
        & (pl.col("decision_date") == pl.lit(args.date).str.to_date())
        & (pl.col("feature_name") == args.feature)
    )
    if selected.height != 1:
        print(json.dumps({"status": "failed", "error": "feature_not_found"}))
        return 4
    print(json.dumps(selected.row(0, named=True), default=str, sort_keys=True))
    return 0


def _evaluate_factor(args: argparse.Namespace, settings: AppSettings) -> int:
    plan = {
        "feature_dataset_version": args.feature_dataset_version,
        "label_dataset_version": args.label_dataset_version,
        "label_version": args.label_version,
        "label_kind": args.label_kind,
        "feature": args.feature,
        "direction": args.direction,
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, "plan": plan}, sort_keys=True))
        return 0
    store = _store(settings)
    features = store.read_dataset_version(
        f"features__{args.feature_set_version}", args.feature_dataset_version
    ).filter(pl.col("feature_name") == args.feature)
    labels = store.read_dataset_version("forward_return_labels", args.label_dataset_version).filter(
        (pl.col("label_version") == args.label_version) & (pl.col("label_kind") == args.label_kind)
    )
    metadata = store.read_dataset_version("universe_metadata", args.universe_metadata_version)
    completeness = _completeness(
        metadata,
        dataset_version=args.feature_dataset_version,
        feature_set_version=args.feature_set_version,
        label_version=args.label_version,
        availability_version=settings.availability.policy_version,
    )
    evaluation = FactorEvaluator().evaluate(
        features,
        labels,
        completeness,
        feature_artifact_version=args.feature_dataset_version,
        tested_factor_inventory=(
            {
                "feature": args.feature,
                "status": "evaluated",
                "feature_artifact_version": args.feature_dataset_version,
            },
        ),
        direction=args.direction,
    )
    report = {
        "summary": evaluation.summary,
        "metadata": evaluation.report_metadata,
        "daily_ic": evaluation.daily_ic.to_dicts(),
        "quantile_returns": evaluation.quantile_returns.to_dicts(),
        "ic_decay": evaluation.ic_decay.to_dicts(),
    }
    encoded = json.dumps(report, default=str, sort_keys=True, indent=2).encode()
    checksum = hashlib.sha256(encoded).hexdigest()
    output = getattr(args, "output", None)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(encoded)
    print(
        json.dumps(
            {
                "status": "ok",
                "classification": completeness.classification,
                "artifact_path": str(output) if output else None,
                "version": args.feature_set_version,
                "checksum": checksum,
            },
            sort_keys=True,
        )
    )
    return 0


def _build_ml_dataset(args: argparse.Namespace, settings: AppSettings) -> int:
    plan = {
        "supervised_version": args.supervised_version,
        "feature_artifact_version": args.feature_artifact_version,
        "label_artifact_version": args.label_artifact_version,
        "universe_version": args.universe_version,
        "dataset_version": args.dataset_version,
        "availability_policy_version": args.availability_policy_version,
        "features": args.features,
        "target_kind": args.target_kind,
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, "plan": plan}, sort_keys=True))
        return 0
    feature_names = tuple(item.strip() for item in args.features.split(",") if item.strip())
    store = _store(settings)
    features = store.read_dataset_version(
        f"features__{args.feature_set_version}", args.feature_artifact_version
    ).filter(pl.col("feature_name").is_in(feature_names))
    labels = store.read_dataset_version(
        "forward_return_labels", args.label_artifact_version
    ).filter(pl.col("label_version") == args.label_version)
    universe = store.read_dataset_version("universe_membership", args.universe_version)
    target = TargetContract(
        kind=args.target_kind,
        label_version=args.label_version,
        return_horizon=args.return_horizon,
        entry_definition=args.entry_definition,
        exit_definition=args.exit_definition,
        return_semantics="gross_before_costs",
        benchmark_definition=args.benchmark_definition,
        rank_direction=args.rank_direction,
    )
    supervised = SupervisedDatasetBuilder().build(
        features,
        labels,
        universe,
        version=args.supervised_version,
        feature_names=feature_names,
        feature_set_version=args.feature_set_version,
        feature_artifact_version=args.feature_artifact_version,
        label_artifact_version=args.label_artifact_version,
        universe_version=args.universe_version,
        dataset_version=args.dataset_version,
        availability_policy_version=args.availability_policy_version,
        target=target,
        missing_feature_policy=MissingFeaturePolicy(args.missing_feature_policy),
    )
    artifact = store.stage_dataset(
        f"ml_supervised__{args.supervised_version}",
        supervised.frame,
        {
            "schema_version": 1,
            "source_checksums": [
                args.feature_artifact_version,
                args.label_artifact_version,
                args.universe_version,
                args.dataset_version,
            ],
            "transformation_algorithm_version": "supervised-exact-time-join-v1",
            "configuration_version": args.supervised_version,
            "configuration": plan,
        },
    )
    manifest_path = write_supervised_manifest(
        settings.research.artifact_root, supervised.manifest, artifact.checksum
    )
    print(
        json.dumps(
            {
                "status": "candidate",
                "artifact_version": artifact.checksum,
                "checksum": supervised.manifest.checksum,
                "manifest_path": str(manifest_path),
                "rows": artifact.row_count,
            },
            sort_keys=True,
        )
    )
    return 0


def _run_ml_experiment(args: argparse.Namespace, settings: AppSettings) -> int:
    plan = {
        "experiment_id": args.experiment_id,
        "supervised_version": args.supervised_version,
        "supervised_artifact_version": args.supervised_artifact_version,
        "calendar_version": args.calendar_version,
        "universe_metadata_version": args.universe_metadata_version,
        "split_method": args.split_method,
        "split_version": args.split_version,
    }
    if args.dry_run:
        print(json.dumps({"dry_run": True, "plan": plan}, sort_keys=True))
        return 0
    store = _store(settings)
    frame = store.read_dataset_version(
        f"ml_supervised__{args.supervised_version}", args.supervised_artifact_version
    )
    manifest_payload = read_supervised_manifest(
        settings.research.artifact_root, args.supervised_artifact_version
    )
    manifest = manifest_from_dict(manifest_payload)
    if manifest.version != args.supervised_version:
        raise ValueError("supervised version does not match its pinned manifest")
    calendar = _canonical_calendar(
        store.read_dataset_version("trading_calendar", args.calendar_version)
    )
    sessions = sorted(calendar.sessions["trade_date"].unique().to_list())
    splitter = WalkForwardSplitter(
        WalkForwardConfig(
            version=args.split_version,
            method=SplitMethod(args.split_method),
            train_sessions=args.train_sessions,
            validation_sessions=args.validation_sessions,
            test_sessions=args.test_sessions,
            step_sessions=args.step_sessions,
            embargo_sessions=args.embargo_sessions,
            final_holdout_sessions=args.final_holdout_sessions,
        ),
        sessions,
    )
    splits = splitter.split(frame)
    if not splits.folds:
        raise ValueError("walk-forward configuration produced no folds")
    metadata = store.read_dataset_version("universe_metadata", args.universe_metadata_version)
    completeness = _completeness(
        metadata,
        dataset_version=manifest.dataset_version,
        feature_set_version=manifest.feature_set_version,
        label_version=manifest.label_version,
        availability_version=manifest.availability_policy_version,
    )
    result = BaselineExperimentRunner(
        random_seed=settings.research.random_seed,
        sample_weight_policy=SampleWeightPolicy(args.sample_weight_policy),
    ).run(
        SupervisedDataset(frame, manifest),
        splits,
        completeness,
        experiment_id=args.experiment_id,
        created_at=datetime.now(UTC),
        bootstrap_block_length=args.bootstrap_block_length,
        bootstrap_resamples=args.bootstrap_resamples,
    )
    checksum, artifact_path = ExperimentStore(settings.research.artifact_root).save(result)
    print(
        json.dumps(
            {
                "status": "candidate",
                "experiment_id": result.experiment_id,
                "artifact_version": checksum,
                "checksum": checksum,
                "artifact_path": str(artifact_path),
                "completeness_status": completeness.classification,
            },
            sort_keys=True,
        )
    )
    return 0


def _inspect_experiment(args: argparse.Namespace, settings: AppSettings) -> int:
    payload = ExperimentStore(settings.research.artifact_root).inspect(
        args.experiment_artifact_version
    )
    print(json.dumps(payload, default=str, sort_keys=True))
    return 0


def _generate_model_card(args: argparse.Namespace, settings: AppSettings) -> int:
    checksum = ExperimentStore(settings.research.artifact_root).write_model_card(
        args.experiment_artifact_version, args.output
    )
    print(
        json.dumps(
            {
                "status": "candidate",
                "experiment_artifact_version": args.experiment_artifact_version,
                "checksum": checksum,
                "artifact_path": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0


def _run_backtest(args: argparse.Namespace, settings: AppSettings) -> int:
    pinned_names = (
        "prediction_artifact_version",
        "model_artifact_version",
        "dataset_version",
        "universe_version",
        "calendar_version",
        "corporate_action_version",
        "benchmark_version",
        "target_policy_version",
    )
    pinned = {name: getattr(args, name) for name in pinned_names}
    if not args.demo:
        missing = [name for name, value in pinned.items() if not value]
        invalid = [name for name, value in pinned.items() if value in {"latest", "current"}]
        if missing or invalid:
            print(
                "configuration error: real backtests require exact pinned versions; "
                f"missing={missing}, invalid={invalid}",
                file=sys.stderr,
            )
            return 2
        if args.dry_run:
            print(json.dumps({"dry_run": True, "pinned_inputs": pinned}, sort_keys=True))
            return 0
        print(
            "configuration error: selected-ledger filesystem adapter is not configured; "
            "no demo fallback was used",
            file=sys.stderr,
        )
        return 2
    from island_quant.backtest.integration import build_demo_backtest_artifact

    if args.dry_run:
        from island_quant.analytics.scenarios import default_scenario_grid

        count = len(default_scenario_grid())
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "mode": "demo",
                    "artifact_namespace": "demo",
                    "scenario_count": count,
                    "estimated_engine_runs": count + 1,
                },
                sort_keys=True,
            )
        )
        return 0
    artifact = build_demo_backtest_artifact(settings.research.artifact_root / "demo", dry_run=False)
    print(
        json.dumps(
            {
                "dry_run": args.dry_run,
                "run_id": artifact.manifest.backtest_run_id,
                "artifact_version": artifact.manifest.artifact_version,
                "event_journal_checksum": artifact.manifest.event_journal_checksum,
                "ledger_checksum": artifact.manifest.ledger_checksum,
                "report_checksum": artifact.manifest.report_checksum,
                "classification": artifact.manifest.classification,
                "artifact_namespace": "demo",
            },
            sort_keys=True,
        )
    )
    return 0


def _artifact_root(args: argparse.Namespace, settings: AppSettings) -> Path:
    return (
        settings.research.artifact_root / "demo"
        if args.artifact_namespace == "demo"
        else settings.research.artifact_root
    )


def _read_backtest(version: str, root: Path) -> dict[str, Any] | None:
    from island_quant.backtest.artifacts import BacktestArtifactStore

    try:
        return BacktestArtifactStore(root).read(version)
    except ValueError:
        print("artifact error: invalid artifact version format", file=sys.stderr)
    except FileNotFoundError:
        print("artifact error: artifact not found", file=sys.stderr)
    except RuntimeError:
        print("artifact error: artifact integrity verification failed", file=sys.stderr)
    return None


def _inspect_backtest(args: argparse.Namespace, settings: AppSettings) -> int:
    payload = _read_backtest(args.artifact_version, _artifact_root(args, settings))
    if payload is None:
        return 2
    print(json.dumps(payload, sort_keys=True, default=str))
    return 0


def _compare_backtests(args: argparse.Namespace, settings: AppSettings) -> int:
    root = _artifact_root(args, settings)
    left = _read_backtest(args.left_version, root)
    right = _read_backtest(args.right_version, root)
    if left is None or right is None:
        return 2
    keys = ("total_return", "turnover", "fees", "taxes", "slippage_cost")
    comparison = {
        key: {
            "left": left["performance_metrics"][key],
            "right": right["performance_metrics"][key],
        }
        for key in keys
    }
    print(json.dumps({"left": args.left_version, "right": args.right_version, **comparison}))
    return 0


def _generate_backtest_report(args: argparse.Namespace, settings: AppSettings) -> int:
    artifact = _read_backtest(args.artifact_version, _artifact_root(args, settings))
    if artifact is None:
        return 2
    artifact_root = settings.research.artifact_root.resolve()
    resolved_output = args.output.resolve()
    if resolved_output.is_relative_to(artifact_root):
        print("report error: output cannot be inside immutable artifact storage", file=sys.stderr)
        return 2
    if args.output.exists() and not args.force:
        print("report error: output exists; use --force to replace it", file=sys.stderr)
        return 2
    manifest = artifact["manifest"]
    metrics = artifact["performance_metrics"]
    escape = _escape_markdown
    report = (
        "# Backtest candidate report\n\n"
        f"- Artifact: `{args.artifact_version}`\n"
        f"- Run: `{escape(str(manifest['backtest_run_id']))}`\n"
        f"- Classification: **{escape(str(manifest['classification']))}**\n"
        f"- PIT status: **{escape(str(manifest['completeness_status']))}**\n"
        f"- Synthetic: `{manifest['synthetic_demo']}`\n"
        f"- Dirty: `{manifest['dirty']}`\n"
        f"- Total return: `{metrics['total_return']}`\n"
        f"- Ending equity: `{metrics['ending_equity']} TWD`\n"
        f"- Fee/tax policy: `{escape(str(manifest['fee_tax_policy_version']))}`\n"
        f"- Execution model: `{escape(str(manifest['execution_model_version']))}`\n"
        f"- Settlement policy: `{escape(str(manifest['settlement_policy_version']))}`\n"
        f"- Reconciliation residual: `{metrics['reconciliation_residual']}`\n\n"
        "> Synthetic/exploratory engineering evidence; not investment performance.\n"
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(json.dumps({"artifact_version": args.artifact_version, "output": args.output.name}))
    return 0


def _escape_markdown(value: str) -> str:
    escaped = (
        f"\\{character}" if character in r"\\`*_{}[]<>#|" else character for character in value
    )
    return "".join(escaped)


def _run_research_pipeline(args: argparse.Namespace) -> int:
    from island_quant.pipeline.exploratory import (
        ExploratoryPipelineConfig,
        RealCacheExploratoryPipeline,
    )

    try:
        pipeline = RealCacheExploratoryPipeline(
            ExploratoryPipelineConfig.load(args.pipeline_config)
        )
        plan = pipeline.plan(confirm_large_run=args.confirm_large_run)
        if args.dry_run:
            print(json.dumps({"dry_run": True, "plan": asdict(plan)}, default=str, sort_keys=True))
            return 0
        result = pipeline.run(confirm_large_run=args.confirm_large_run)
    except (OSError, ValueError, RuntimeError, yaml.YAMLError) as exc:
        print(f"pipeline error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(result), default=str, sort_keys=True))
    return 0 if result.status == "completed" else 2


def _inspect_pipeline_run(args: argparse.Namespace, settings: AppSettings) -> int:
    from island_quant.pipeline.orchestration import CheckpointedPipeline

    try:
        payload = CheckpointedPipeline(settings.research.artifact_root, Path("state")).inspect(
            args.run_version
        )
    except ValueError:
        print("pipeline error: invalid run version format", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print("pipeline error: run not found", file=sys.stderr)
        return 2
    except RuntimeError:
        print("pipeline error: run integrity verification failed", file=sys.stderr)
        return 2
    print(json.dumps(payload, default=str, sort_keys=True))
    return 0


def _dashboard(args: argparse.Namespace, settings: AppSettings) -> int:
    selected_modes = sum(
        (bool(args.demo), bool(args.artifact_version), bool(args.pipeline_run_version))
    )
    if selected_modes != 1:
        print(
            "configuration error: dashboard requires --demo, --artifact-version, or "
            "--pipeline-run-version "
            "(exactly one)",
            file=sys.stderr,
        )
        return 2
    if not 1 <= args.port <= 65535:
        print("configuration error: dashboard port must be between 1 and 65535", file=sys.stderr)
        return 2
    import uvicorn

    if args.demo:
        application: Any = "island_quant.dashboard.app:app"
        mode = "demo"
    elif args.pipeline_run_version:
        if args.reload:
            print("configuration error: pipeline dashboard forbids --reload", file=sys.stderr)
            return 2
        from island_quant.backtest.artifacts import BacktestArtifactStore
        from island_quant.dashboard.app import create_app
        from island_quant.dashboard.backtests import BacktestArtifactQuery
        from island_quant.dashboard.pipelines import PipelineArtifactQuery
        from island_quant.dashboard.service import DashboardQueryService

        try:
            pipeline_adapter = PipelineArtifactQuery(
                settings.research.artifact_root, args.pipeline_run_version
            )
        except ValueError:
            print("pipeline error: invalid run version format", file=sys.stderr)
            return 2
        except FileNotFoundError:
            print("pipeline error: run not found", file=sys.stderr)
            return 2
        except RuntimeError:
            print("pipeline error: run integrity verification failed", file=sys.stderr)
            return 2
        backtest_version = pipeline_adapter.status().get("backtest_artifact_version")
        backtest_adapter = (
            BacktestArtifactQuery(
                BacktestArtifactStore(settings.research.artifact_root),
                str(backtest_version),
            )
            if backtest_version
            else None
        )
        application = create_app(
            DashboardQueryService(
                pipeline_query=pipeline_adapter, backtest_query=backtest_adapter
            )
        )
        mode = f"pipeline {args.pipeline_run_version}"
    else:
        if args.reload:
            print("configuration error: artifact dashboard forbids --reload", file=sys.stderr)
            return 2
        from island_quant.backtest.artifacts import BacktestArtifactStore
        from island_quant.dashboard.app import create_app
        from island_quant.dashboard.backtests import BacktestArtifactQuery
        from island_quant.dashboard.service import DashboardQueryService

        try:
            adapter = BacktestArtifactQuery(
                BacktestArtifactStore(_artifact_root(args, settings)), args.artifact_version
            )
        except ValueError:
            print("artifact error: invalid artifact version format", file=sys.stderr)
            return 2
        except FileNotFoundError:
            print("artifact error: artifact not found", file=sys.stderr)
            return 2
        except RuntimeError:
            print("artifact error: artifact integrity verification failed", file=sys.stderr)
            return 2
        application = create_app(DashboardQueryService(backtest_query=adapter))
        mode = f"artifact {args.artifact_version}"
    print(
        f"Island Quant {mode} dashboard: http://{args.host}:{args.port}\n"
        "READ ONLY / NOT FOR LIVE TRADING\nPress Ctrl+C to stop."
    )
    uvicorn.run(application, host=args.host, port=args.port, reload=args.reload, access_log=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
