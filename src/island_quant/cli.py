"""Phase 0 operational commands."""

from __future__ import annotations

import argparse
import json
import os
import sys
from importlib.metadata import version
from pathlib import Path

import yaml
from pydantic import ValidationError

from island_quant.config import AppSettings, load_settings
from island_quant.data.adapters.finmind import FinMindProvider
from island_quant.data.adapters.fixture import FixtureProvider
from island_quant.data.ingestion import IngestionService
from island_quant.data.ports import HistoricalDataProvider
from island_quant.data.universe import (
    INCOMPLETE_UNIVERSE_WARNING,
    UniversePolicy,
    require_research_complete,
)
from island_quant.data.validation import DataValidationError, validate_daily_prices
from island_quant.logging import configure_logging
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
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
