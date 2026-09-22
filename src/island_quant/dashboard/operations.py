"""Read-only paper operations dashboard query adapter."""

from __future__ import annotations

from datetime import datetime, timedelta

from island_quant.dashboard.models import DashboardContext
from island_quant.oms.repository import SQLiteOMSRepository
from island_quant.operations.monitoring import OperationsStateStore
from island_quant.operations.scheduler import PaperScheduler


class PaperOperationsQuery:
    def __init__(
        self,
        repository: SQLiteOMSRepository,
        state: OperationsStateStore,
        scheduler: PaperScheduler,
    ) -> None:
        self.repository = repository
        self.state = state
        self.scheduler = scheduler

    def status(self, at: datetime) -> dict[str, object]:
        snapshot = self.state.snapshot(at, timedelta(minutes=2))
        portfolio = self.state.current_portfolio()
        return {
            **snapshot,
            "context": DashboardContext(
                environment="PAPER",
                banner="PAPER / READ ONLY / NOT LIVE / NO BROKER CONNECTION",
                dataset_version="paper-state-v1",
                pit_completeness="operational",
                last_artifact_update=at,
                universes=("Paper account",),
                selected_universe="Paper account",
                selected_date_range="persistent state",
            ),
            "badge": "PAPER / READ ONLY / NOT LIVE",
            "orders": [
                {
                    "order_id": item.order_id,
                    "instrument_id": item.instrument_id,
                    "side": item.side,
                    "quantity": item.quantity,
                    "filled_quantity": item.filled_quantity,
                    "state": item.state.value,
                    "updated_at": item.updated_at.isoformat(),
                }
                for item in self.repository.list_orders()
            ],
            "fills": list(self.repository.fills()),
            "jobs": list(self.scheduler.runs()),
            "positions": portfolio["positions"] if portfolio is not None else [],
            "cash_nav": (
                {
                    "status": "available",
                    "cash": portfolio["cash"],
                    "available_cash": portfolio["available_cash"],
                    "nav": portfolio["nav"],
                    "valuation_complete": portfolio["valuation_complete"],
                    "as_of": portfolio["as_of"],
                }
                if portfolio is not None
                else {"status": "unavailable", "reason": "portfolio projection not published"}
            ),
            "risk": {
                "kill_new_risk": bool(
                    snapshot["service"]["safe_mode"]  # type: ignore[index]
                )
            },
            "reconciliation": {
                "status": (
                    snapshot["metrics"]["reconciliation_mismatches"]["value"]
                    if isinstance(snapshot["metrics"], dict)
                    else "unavailable"
                )
            },
            "audit_timeline": [
                {
                    "order_id": order.order_id,
                    "events": [
                        event.reason_code for event in self.repository.events(order.order_id)
                    ],
                }
                for order in self.repository.list_orders()
            ],
        }
