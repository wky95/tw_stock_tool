"""Paper runtime CLI composition with pinned local fixtures only."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal

from island_quant.brokers.paper import (
    DeterministicPaperBroker,
    PaperBrokerPolicy,
    PaperMarketEvent,
)
from island_quant.config import AppSettings
from island_quant.domain.models import Market
from island_quant.oms.repository import SQLiteOMSRepository
from island_quant.oms.service import PaperOMSService
from island_quant.operations.accounting import (
    PaperAccountingPolicy,
    PaperAccountingProjector,
    PaperInstrumentReference,
    PaperMark,
)
from island_quant.operations.monitoring import OperationsStateStore
from island_quant.operations.promotion import PaperPromotionRequest, PaperTargetPromoter
from island_quant.operations.runtime import PaperRuntime, runtime_result_dict
from island_quant.operations.scheduler import FixedClock, PaperScheduler
from island_quant.operations.strategy import (
    MARKET_TIMEZONE,
    ExactPaperTargetReader,
    PaperStrategyRiskPolicy,
    PaperTargetExecutor,
)


def add_runtime_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser("paper-service-run")
    parser.add_argument("--paper", action="store_true", required=True)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--target-artifact-version")
    promotion = subparsers.add_parser("paper-promote-target")
    promotion.add_argument("--paper", action="store_true", required=True)
    promotion.add_argument("--source-version", required=True)
    promotion.add_argument("--reviewer", required=True)
    promotion.add_argument("--reason", required=True)
    promotion.add_argument("--approved-at", required=True)
    promotion.add_argument("--execution-session", required=True)
    promotion.add_argument("--approve-pit", action="store_true")
    promotion.add_argument("--approve-data-license", action="store_true")
    promotion.add_argument("--approve-risk", action="store_true")
    promotion.add_argument("--dry-run", action="store_true")
    promotion.add_argument("--confirm", action="store_true")


def run_promotion(args: argparse.Namespace, settings: AppSettings) -> int:
    try:
        if args.dry_run and args.confirm:
            raise ValueError("paper promotion accepts either --dry-run or --confirm")
        if not args.dry_run and not args.confirm:
            print("paper promotion error: use --dry-run or --confirm", file=sys.stderr)
            return 2
        request = PaperPromotionRequest(
            args.source_version,
            args.reviewer,
            args.reason,
            datetime.fromisoformat(args.approved_at),
            date.fromisoformat(args.execution_session),
            args.approve_pit,
            args.approve_data_license,
            args.approve_risk,
        )
        result = PaperTargetPromoter(settings.research.artifact_root).promote(
            request, dry_run=bool(args.dry_run)
        )
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(f"paper promotion error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(result), default=str, sort_keys=True))
    return 0


def run_runtime(args: argparse.Namespace, settings: AppSettings) -> int:
    try:
        return _run_runtime(args, settings)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(f"paper runtime error: {exc}", file=sys.stderr)
        return 2


def _run_runtime(args: argparse.Namespace, settings: AppSettings) -> int:
    as_of = datetime.fromisoformat(args.as_of)
    if as_of.tzinfo is None:
        raise ValueError("--as-of must be timezone-aware")
    session = date.fromisoformat(args.session)
    market_payload = json.loads(settings.paper.market_fixture_path.read_text(encoding="utf-8"))
    instrument_payload = json.loads(
        settings.paper.instrument_fixture_path.read_text(encoding="utf-8")
    )
    calendar_payload = json.loads(settings.paper.calendar_fixture_path.read_text(encoding="utf-8"))
    market = tuple(
        PaperMarketEvent(
            str(item["instrument_id"]),
            datetime.fromisoformat(str(item["event_time"])),
            Decimal(str(item["reference_price"])),
            int(item["volume"]),
            bool(item.get("suspended", False)),
            bool(item.get("limit_locked", False)),
        )
        for item in market_payload
    )
    instruments = tuple(
        PaperInstrumentReference(
            str(item["instrument_id"]),
            Market(str(item["market"])),
            str(item["reference_version"]),
        )
        for item in instrument_payload
    )
    sessions = tuple(date.fromisoformat(str(item)) for item in calendar_payload)
    if session not in sessions:
        raise ValueError("paper service session is absent from pinned calendar")
    target_reader = (
        ExactPaperTargetReader(settings.research.artifact_root)
        if args.target_artifact_version is not None
        else None
    )
    target_snapshot = (
        target_reader.read(args.target_artifact_version, session=session, as_of=as_of)
        if target_reader is not None
        else None
    )
    if args.dry_run:
        print(
            json.dumps(
                {
                    "environment": "PAPER",
                    "dry_run": True,
                    "session": args.session,
                    "as_of": args.as_of,
                    "jobs": 10,
                    "live_trading_enabled": False,
                    "target_artifact_version": (
                        target_snapshot.artifact_version if target_snapshot is not None else None
                    ),
                },
                sort_keys=True,
            )
        )
        return 0
    oms = SQLiteOMSRepository(settings.paper.oms_database_path)
    state = OperationsStateStore(settings.paper.operations_database_path)
    scheduler = PaperScheduler(
        settings.paper.scheduler_database_path, FixedClock(as_of), owner="paper-service-cli"
    )
    oms.initialize()
    state.initialize()
    scheduler.initialize()
    broker = DeterministicPaperBroker(market, PaperBrokerPolicy())
    projector = PaperAccountingProjector(
        oms,
        state,
        PaperAccountingPolicy(initial_cash=settings.trading.initial_cash),
        instruments,
        sessions,
        tuple(
            PaperMark(
                item.instrument_id,
                item.event_time.astimezone(MARKET_TIMEZONE).date(),
                item.reference_price,
                item.event_time,
                "pinned_paper_market_event",
            )
            for item in market
        ),
    )
    service = PaperOMSService(oms)
    target_executor = (
        PaperTargetExecutor(
            oms,
            service,
            {item.instrument_id: item.market for item in instruments},
            {item.instrument_id: item.reference_price for item in market},
            {item.instrument_id: item.volume for item in market},
            PaperStrategyRiskPolicy(
                version=settings.paper.strategy_risk_policy_version,
                initial_cash=settings.trading.initial_cash,
                maximum_gross_exposure=settings.trading.maximum_gross_exposure,
                maximum_single_position=settings.trading.maximum_position_weight,
                estimated_adverse_slippage_bps=(
                    settings.paper.estimated_adverse_slippage_bps
                ),
                minimum_cash_buffer=settings.paper.minimum_cash_buffer,
                maximum_turnover=settings.paper.maximum_turnover,
                maximum_volume_participation=(
                    settings.paper.maximum_volume_participation
                ),
                maximum_position_count=settings.paper.maximum_position_count,
            ),
        )
        if target_reader is not None
        else None
    )
    runtime = PaperRuntime(
        oms,
        service,
        broker,
        state,
        scheduler,
        projector,
        settings.research.artifact_root,
        market,
        target_reader=target_reader,
        target_executor=target_executor,
        target_artifact_version=args.target_artifact_version,
    )
    result = runtime.run_once(args.session, as_of)
    print(json.dumps(runtime_result_dict(result), default=str, sort_keys=True))
    return 2 if result.safe_mode else 0
