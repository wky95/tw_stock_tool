"""Fail-closed OMS/broker reconciliation classifications."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from island_quant.brokers.paper import DeterministicPaperBroker
from island_quant.oms.models import TERMINAL_STATES, OMSState
from island_quant.oms.repository import SQLiteOMSRepository


@dataclass(frozen=True, slots=True)
class ReconciliationMismatch:
    category: str
    order_id: str
    critical: bool
    detail: str


@dataclass(frozen=True, slots=True)
class OMSReconciliationReport:
    status: str
    mismatches: tuple[ReconciliationMismatch, ...]
    kill_new_risk: bool


@dataclass(frozen=True, slots=True)
class ReconciliationSnapshot:
    local_positions: tuple[tuple[str, int], ...] = ()
    broker_positions: tuple[tuple[str, int], ...] = ()
    local_cash: Decimal = Decimal("0")
    broker_cash: Decimal = Decimal("0")
    local_reservations: Decimal = Decimal("0")
    broker_fill_ids: tuple[str, ...] = ()


def reconcile_oms(
    repository: SQLiteOMSRepository,
    broker: DeterministicPaperBroker,
    snapshot: ReconciliationSnapshot | None = None,
) -> OMSReconciliationReport:
    mismatches: list[ReconciliationMismatch] = []
    local_by_client = {order.client_order_id: order for order in repository.list_orders()}
    for client_order_id in broker.requests:
        if client_order_id not in local_by_client:
            mismatches.append(
                ReconciliationMismatch(
                    "unknown_broker_order",
                    client_order_id,
                    True,
                    "broker order has no local OMS order",
                )
            )
    for client_order_id, order in local_by_client.items():
        if (
            order.state not in TERMINAL_STATES
            and order.state
            not in {
                OMSState.CREATED,
                OMSState.RISK_PENDING,
                OMSState.RISK_APPROVED,
                OMSState.SUBMIT_PENDING,
            }
            and client_order_id not in broker.requests
        ):
            mismatches.append(
                ReconciliationMismatch(
                    "missing_broker_order",
                    order.order_id,
                    True,
                    "submitted local order missing at broker",
                )
            )
        events = repository.events(order.order_id)
        if events and events[-1].sequence != order.sequence:
            mismatches.append(
                ReconciliationMismatch(
                    "local_sequence_mismatch",
                    order.order_id,
                    True,
                    "order and journal sequence differ",
                )
            )
    if snapshot is not None:
        if dict(snapshot.local_positions) != dict(snapshot.broker_positions):
            mismatches.append(
                ReconciliationMismatch(
                    "position_mismatch", "portfolio", True, "local and broker positions differ"
                )
            )
        if snapshot.local_cash != snapshot.broker_cash:
            mismatches.append(
                ReconciliationMismatch("cash_mismatch", "cash", True, "cash balances differ")
            )
        local_fill_ids = {str(item["fill_id"]) for item in repository.fills()}
        if local_fill_ids != set(snapshot.broker_fill_ids):
            mismatches.append(
                ReconciliationMismatch(
                    "fill_mismatch", "fills", True, "local and broker fill identities differ"
                )
            )
        if snapshot.local_reservations < 0:
            mismatches.append(
                ReconciliationMismatch(
                    "reservation_mismatch", "cash", True, "local reservation is negative"
                )
            )
    critical = any(item.critical for item in mismatches)
    return OMSReconciliationReport(
        "failed" if mismatches else "passed", tuple(mismatches), critical
    )
