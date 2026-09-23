"""Read-only paper operations dashboard query adapter."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast

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
        target = self.state.current_target_snapshot()
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
            "target_tracking": self._target_tracking(target, portfolio),
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

    @staticmethod
    def _target_tracking(
        target: dict[str, object] | None, portfolio: dict[str, object] | None
    ) -> dict[str, object]:
        if target is None:
            return {"status": "unavailable", "reason": "no promoted paper target selected"}
        raw_targets = target.get("targets")
        if not isinstance(raw_targets, list):
            raise RuntimeError("persisted paper target projection is malformed")
        positions: dict[str, int] = {}
        marks: dict[str, Decimal] = {}
        nav = Decimal("0")
        valuation_complete = False
        if portfolio is not None:
            raw_positions = cast(list[dict[str, object]], portfolio.get("positions", []))
            positions = {
                str(item["instrument_id"]): int(str(item["quantity"]))
                for item in raw_positions
            }
            raw_marks = cast(list[list[object]], portfolio.get("marks", []))
            marks = {str(item[0]): Decimal(str(item[1])) for item in raw_marks}
            nav = Decimal(str(portfolio["nav"]))
            valuation_complete = bool(portfolio.get("valuation_complete"))
        rows: list[dict[str, object]] = []
        total_drift = Decimal("0")
        for raw in raw_targets:
            if not isinstance(raw, dict):
                raise RuntimeError("persisted paper target member is malformed")
            key = str(raw["instrument_key"])
            quantity = positions.get(key, 0)
            mark = marks.get(key)
            actual_weight = None
            if valuation_complete and nav > 0:
                if quantity == 0:
                    actual_weight = Decimal("0")
                elif mark is not None:
                    actual_weight = Decimal(quantity) * mark / nav
            target_weight = Decimal(str(raw["target_weight"]))
            drift = abs(actual_weight - target_weight) if actual_weight is not None else None
            if drift is not None:
                total_drift += drift
            rows.append(
                {
                    "instrument_id": key,
                    "target_weight": str(target_weight),
                    "actual_weight": str(actual_weight) if actual_weight is not None else None,
                    "drift": str(drift) if drift is not None else None,
                    "actual_quantity": quantity,
                    "mark": str(mark) if mark is not None else None,
                }
            )
        return {
            "status": "available" if valuation_complete else "valuation_incomplete",
            "artifact_version": target["artifact_version"],
            "decision_time": target["decision_time"],
            "execution_session": target["execution_session"],
            "total_absolute_drift": str(total_drift) if valuation_complete else None,
            "rows": rows,
        }
