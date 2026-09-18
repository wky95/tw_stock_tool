"""Phase 0 operational commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from importlib.metadata import version
from pathlib import Path

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


def _ingest_data(args: argparse.Namespace, settings: AppSettings) -> int:
    provider_name = args.provider or settings.data.provider
    symbols = (
        [item.strip() for item in args.symbols.split(",") if item.strip()]
        if args.symbols
        else None
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
    metadata_frame = store.read_dataset_version(
        "universe_metadata", args.universe_metadata_version
    )
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
        unsupported_corporate_action_count=int(
            row.get("unsupported_corporate_action_count", 1)
        ),
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
    labels = store.read_dataset_version(
        "forward_return_labels", args.label_dataset_version
    ).filter(
        (pl.col("label_version") == args.label_version)
        & (pl.col("label_kind") == args.label_kind)
    )
    metadata = store.read_dataset_version(
        "universe_metadata", args.universe_metadata_version
    )
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


if __name__ == "__main__":
    raise SystemExit(main())
