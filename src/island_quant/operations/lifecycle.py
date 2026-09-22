"""Fail-closed paper service lifecycle and dependency health."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from island_quant.brokers.paper import DeterministicPaperBroker
from island_quant.oms.reconciliation import reconcile_oms
from island_quant.oms.repository import OMSPersistenceError, SQLiteOMSRepository
from island_quant.operations.monitoring import AlertLevel, OperationsStateStore


@dataclass(frozen=True, slots=True)
class ServiceHealth:
    live: bool
    ready: bool
    safe_mode: bool
    reason: str | None


class PaperServiceLifecycle:
    def __init__(
        self,
        repository: SQLiteOMSRepository,
        broker: DeterministicPaperBroker,
        state: OperationsStateStore,
    ) -> None:
        self.repository = repository
        self.broker = broker
        self.state = state
        self.started = False

    def startup(self, at: datetime) -> ServiceHealth:
        self.state.initialize()
        if not self.state.record_start(at, window=timedelta(minutes=5), maximum=3):
            self.state.enter_safe_mode("crash_loop_detected")
            return ServiceHealth(True, False, True, "crash_loop_detected")
        try:
            self.repository.initialize()
            reconciliation = reconcile_oms(self.repository, self.broker)
        except OMSPersistenceError:
            self.state.enter_safe_mode("database_corruption_or_unavailable")
            self.state.raise_alert(
                AlertLevel.CRITICAL,
                "database_corruption",
                "paper OMS database failed startup integrity check",
                at,
                cooldown=timedelta(hours=1),
            )
            return ServiceHealth(True, False, True, "database_corruption_or_unavailable")
        if reconciliation.kill_new_risk:
            self.state.enter_safe_mode("startup_reconciliation_failed")
            return ServiceHealth(True, False, True, "startup_reconciliation_failed")
        self.state.heartbeat(at)
        self.started = True
        snapshot = self.state.snapshot(at, timedelta(minutes=2))
        service = snapshot["service"]
        assert isinstance(service, dict)
        if service["reason"] == "not_started":
            self.state.resume("startup checks passed")
            snapshot = self.state.snapshot(at, timedelta(minutes=2))
            service = snapshot["service"]
            assert isinstance(service, dict)
        return ServiceHealth(True, not bool(service["safe_mode"]), bool(service["safe_mode"]), None)

    def shutdown(self, at: datetime, *, pending_work: bool) -> ServiceHealth:
        self.state.heartbeat(at)
        self.started = False
        if pending_work:
            self.state.enter_safe_mode("shutdown_with_pending_work")
            return ServiceHealth(False, False, True, "shutdown_with_pending_work")
        return ServiceHealth(False, False, False, None)

    def health(self, at: datetime) -> ServiceHealth:
        snapshot = self.state.snapshot(at, timedelta(minutes=2))
        service = snapshot["service"]
        assert isinstance(service, dict)
        stale = bool(service["heartbeat_stale"])
        safe = bool(service["safe_mode"]) or stale
        reason = "stale_heartbeat" if stale else (str(service["reason"]) if safe else None)
        return ServiceHealth(self.started, self.started and not safe, safe, reason)
