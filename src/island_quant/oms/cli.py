"""Explicit paper-only CLI handlers."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from island_quant.brokers.paper import (
    DeterministicPaperBroker,
    PaperBrokerPolicy,
    PaperMarketEvent,
)
from island_quant.config import AppSettings
from island_quant.oms.models import TERMINAL_STATES, OMSState
from island_quant.oms.reconciliation import reconcile_oms
from island_quant.oms.repository import SQLiteOMSRepository
from island_quant.oms.service import PaperOMSService, PaperOrderCommand


def add_paper_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    for name in (
        "paper-init",
        "paper-status",
        "paper-orders",
        "paper-reconcile",
        "paper-cancel-all",
        "paper-recover",
    ):
        parser = subparsers.add_parser(name)
        parser.add_argument("--paper", action="store_true", required=True)
        if name in {"paper-cancel-all", "paper-recover"}:
            parser.add_argument("--dry-run", action="store_true")
            parser.add_argument("--confirm", action="store_true")
        if name == "paper-recover":
            parser.add_argument("--order-id", required=True)
            parser.add_argument(
                "--state",
                required=True,
                choices=("Submitted", "Acknowledged", "Cancelled", "Rejected"),
            )
            parser.add_argument("--reason", required=True)
    run = subparsers.add_parser("paper-run")
    run.add_argument("--paper", action="store_true", required=True)
    run.add_argument("--client-order-id", required=True)
    run.add_argument("--idempotency-key", required=True)
    run.add_argument("--instrument", required=True)
    run.add_argument("--side", choices=("buy", "sell"), required=True)
    run.add_argument("--quantity", type=int, required=True)
    run.add_argument("--estimated-price", required=True)
    run.add_argument("--available-cash", required=True)
    run.add_argument("--created-at", required=True)


def run_paper_command(args: argparse.Namespace, settings: AppSettings) -> int:
    repository = SQLiteOMSRepository(settings.paper.oms_database_path)
    repository.initialize()
    service = PaperOMSService(repository)
    if args.command == "paper-init":
        return _print({"environment": "PAPER", "status": "initialized"})
    if args.command == "paper-status":
        return _print(
            {
                "environment": "PAPER",
                "live_trading_enabled": False,
                "orders": len(repository.list_orders()),
                "pending_outbox": len(repository.pending_outbox()),
            }
        )
    if args.command == "paper-orders":
        return _print(
            {"environment": "PAPER", "orders": [asdict(x) for x in repository.list_orders()]}
        )
    if args.command == "paper-reconcile":
        report = reconcile_oms(repository, _broker(settings.paper.market_fixture_path))
        _print(asdict(report))
        return 0 if report.status == "passed" else 2
    if args.command == "paper-run":
        created_at = datetime.fromisoformat(args.created_at)
        command = PaperOrderCommand(
            args.client_order_id,
            args.idempotency_key,
            args.instrument,
            args.side,
            args.quantity,
            Decimal(args.estimated_price),
            Decimal(args.available_cash),
            created_at,
        )
        order = service.queue(command)
        if order.state is OMSState.SUBMIT_PENDING:
            service.process_outbox(_broker(settings.paper.market_fixture_path))
            order = repository.get(order.order_id)
        return _print({"environment": "PAPER", "order": asdict(order)})
    if args.command == "paper-cancel-all":
        candidates = [x for x in repository.list_orders() if x.state not in TERMINAL_STATES]
        if args.dry_run or not args.confirm:
            _print({"dry_run": True, "candidate_order_ids": [x.order_id for x in candidates]})
            return 0 if args.dry_run else 2
        now = max((x.updated_at for x in candidates), default=datetime.now().astimezone())
        changed: list[str] = []
        for order in candidates:
            if order.state in {
                OMSState.SUBMITTED,
                OMSState.ACKNOWLEDGED,
                OMSState.PARTIALLY_FILLED,
            }:
                service.request_cancel(order.order_id, now)
                changed.append(order.order_id)
        return _print({"environment": "PAPER", "cancel_pending": changed})
    if args.command == "paper-recover":
        if args.dry_run or not args.confirm:
            _print({"dry_run": True, "order_id": args.order_id, "state": args.state})
            return 0 if args.dry_run else 2
        order = repository.get(args.order_id)
        recovered = service.recover_unknown(
            order.order_id, OMSState(args.state), args.reason, order.updated_at
        )
        return _print({"environment": "PAPER", "order": asdict(recovered)})
    return 2


def _broker(path: Path) -> DeterministicPaperBroker:
    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    market = tuple(
        PaperMarketEvent(
            str(item["instrument_id"]),
            datetime.fromisoformat(str(item["event_time"])),
            Decimal(str(item["reference_price"])),
            int(item["volume"]),
            bool(item.get("suspended", False)),
            bool(item.get("limit_locked", False)),
        )
        for item in payload
    )
    return DeterministicPaperBroker(market, PaperBrokerPolicy())


def _print(value: object) -> int:
    print(json.dumps(value, default=str, sort_keys=True))
    return 0
