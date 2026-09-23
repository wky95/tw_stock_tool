"""Offline broker contract fixture.

This adapter is an engineering fault-injection harness.  Its behavior is not a
description or emulation of Shioaji, SinoPac, TWSE, or TPEx behavior.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from island_quant.brokers.contracts import (
    BrokerCapabilities,
    BrokerCapability,
    BrokerCommandResult,
    BrokerError,
    BrokerErrorCategory,
    BrokerEvent,
    BrokerFillEvent,
    BrokerOrderSnapshot,
    BrokerOrderState,
    BrokerSessionHealth,
    CancelOrderRequest,
    CommandOutcome,
    CredentialReference,
    OrderLot,
    RateLimit,
    ReconciliationSnapshot,
    ReplaceOrderRequest,
    RetryDirective,
    SessionHealthState,
    SubmitOrderRequest,
)


class FixtureFault(StrEnum):
    LOST_RESPONSE = "lost_response"
    RATE_LIMITED = "rate_limited"
    SESSION_EXPIRED = "session_expired"
    ODD_LOT_REJECTED = "odd_lot_rejected"
    SUSPENDED = "suspended"
    LIMIT_LOCKED = "limit_locked"


@dataclass(frozen=True, slots=True)
class FixtureOrder:
    request: SubmitOrderRequest
    snapshot: BrokerOrderSnapshot


class CorruptedBrokerPayload(RuntimeError):
    pass


class OfflineBrokerFixture:
    """In-memory deterministic adapter used only by offline conformance tests."""

    def __init__(
        self,
        *,
        now: datetime | None = None,
        faults: frozenset[FixtureFault] = frozenset(),
        supported: frozenset[BrokerCapability] | None = None,
    ) -> None:
        self.now = now or datetime(2025, 1, 2, 9, 0, tzinfo=UTC)
        self.faults = faults
        self._supported = frozenset(BrokerCapability) if supported is None else supported
        self._health = BrokerSessionHealth(
            SessionHealthState.DISCONNECTED, self.now, None, True, ("not_open",)
        )
        self.orders: dict[str, FixtureOrder] = {}
        self._idempotency: dict[str, str] = {}
        self._events: dict[str, BrokerEvent] = {}
        self._last_sequence: dict[str, int] = {}
        self._fills: dict[str, BrokerFillEvent] = {}
        self.kill_new_risk = True

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            "offline_fixture",
            "1",
            "fixture-1",
            self._supported,
            (RateLimit("orders", 5, 1, 5, self.now),),
        )

    def open(self, credential: CredentialReference) -> BrokerSessionHealth:
        del credential
        if FixtureFault.SESSION_EXPIRED in self.faults:
            self._health = BrokerSessionHealth(
                SessionHealthState.EXPIRED,
                self.now,
                "fixture-session",
                True,
                ("fixture_session_expired",),
            )
        else:
            self.kill_new_risk = False
            self._health = BrokerSessionHealth(
                SessionHealthState.HEALTHY, self.now, "fixture-session", False
            )
        return self._health

    def close(self) -> BrokerSessionHealth:
        self.kill_new_risk = True
        self._health = BrokerSessionHealth(
            SessionHealthState.DISCONNECTED, self.now, None, True, ("closed",)
        )
        return self._health

    def health(self) -> BrokerSessionHealth:
        return replace(self._health, kill_new_risk=self.kill_new_risk)

    def submit(self, request: SubmitOrderRequest) -> BrokerCommandResult:
        if self.kill_new_risk or self._health.state is not SessionHealthState.HEALTHY:
            return self._rejected(request.client_order_id, "KILL_NEW_RISK")
        if BrokerCapability.SUBMIT not in self._supported:
            return self._rejected(
                request.client_order_id,
                "UNSUPPORTED_CAPABILITY",
                BrokerErrorCategory.UNSUPPORTED,
            )
        existing_client = self._idempotency.get(request.idempotency_key)
        if existing_client is not None:
            existing = self.orders[existing_client]
            if existing.request != request:
                return self._rejected(
                    request.client_order_id,
                    "IDEMPOTENCY_CONFLICT",
                    BrokerErrorCategory.CONFLICT,
                )
            return self._accepted(existing.snapshot)
        if FixtureFault.RATE_LIMITED in self.faults:
            return self._rejected(
                request.client_order_id,
                "RATE_LIMITED",
                BrokerErrorCategory.RATE_LIMITED,
                RetryDirective.BACKOFF,
            )
        if request.order_lot is not OrderLot.COMMON and (
            FixtureFault.ODD_LOT_REJECTED in self.faults
        ):
            return self._rejected(request.client_order_id, "ODD_LOT_REJECTED")
        if FixtureFault.SUSPENDED in self.faults:
            return self._rejected(request.client_order_id, "INSTRUMENT_SUSPENDED")
        if FixtureFault.LIMIT_LOCKED in self.faults:
            return self._rejected(request.client_order_id, "PRICE_LIMIT_LOCKED")

        broker_id = hashlib.sha256(f"fixture:{request.client_order_id}".encode()).hexdigest()[:16]
        snapshot = BrokerOrderSnapshot(
            request.client_order_id,
            broker_id,
            BrokerOrderState.ACKNOWLEDGED,
            request.quantity,
            0,
            1,
        )
        self.orders[request.client_order_id] = FixtureOrder(request, snapshot)
        self._idempotency[request.idempotency_key] = request.client_order_id
        self._last_sequence[request.client_order_id] = 1
        if FixtureFault.LOST_RESPONSE in self.faults:
            return BrokerCommandResult(
                CommandOutcome.UNKNOWN,
                request.client_order_id,
                self.now,
                broker_id,
                BrokerError(
                    1,
                    BrokerErrorCategory.TIMEOUT,
                    "SUBMIT_OUTCOME_UNKNOWN",
                    "fixture recorded the order but withheld the response",
                    RetryDirective.RECONCILE_THEN_DECIDE,
                ),
            )
        return self._accepted(snapshot)

    def cancel(self, request: CancelOrderRequest) -> BrokerCommandResult:
        if BrokerCapability.CANCEL not in self._supported:
            return self._rejected(
                request.client_order_id,
                "UNSUPPORTED_CAPABILITY",
                BrokerErrorCategory.UNSUPPORTED,
            )
        order = self.orders.get(request.client_order_id)
        if order is None:
            return self._rejected(
                request.client_order_id, "ORDER_NOT_FOUND", BrokerErrorCategory.NOT_FOUND
            )
        snapshot = order.snapshot
        if snapshot.state is BrokerOrderState.FILLED:
            return self._rejected(request.client_order_id, "ALREADY_FILLED")
        cancelled = replace(
            snapshot,
            state=BrokerOrderState.CANCELLED,
            last_sequence=snapshot.last_sequence + 1,
        )
        self.orders[request.client_order_id] = FixtureOrder(order.request, cancelled)
        self._last_sequence[request.client_order_id] = cancelled.last_sequence
        return self._accepted(cancelled)

    def replace(self, request: ReplaceOrderRequest) -> BrokerCommandResult:
        if BrokerCapability.REPLACE not in self._supported:
            return self._rejected(
                request.client_order_id,
                "UNSUPPORTED_CAPABILITY",
                BrokerErrorCategory.UNSUPPORTED,
            )
        order = self.orders.get(request.client_order_id)
        if order is None:
            return self._rejected(
                request.client_order_id, "ORDER_NOT_FOUND", BrokerErrorCategory.NOT_FOUND
            )
        snapshot = order.snapshot
        if snapshot.state is BrokerOrderState.FILLED:
            return self._rejected(request.client_order_id, "ALREADY_FILLED")
        if request.new_quantity < snapshot.filled_quantity:
            return self._rejected(request.client_order_id, "QUANTITY_BELOW_FILLED")
        replaced_snapshot = replace(
            snapshot,
            state=BrokerOrderState.REPLACED,
            quantity=request.new_quantity,
            last_sequence=snapshot.last_sequence + 1,
        )
        replaced_request = replace(
            order.request,
            quantity=request.new_quantity,
            limit_price=request.new_limit_price,
        )
        self.orders[request.client_order_id] = FixtureOrder(
            replaced_request, replaced_snapshot
        )
        self._last_sequence[request.client_order_id] = replaced_snapshot.last_sequence
        return self._accepted(replaced_snapshot)

    def query_order(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        order = self.orders.get(client_order_id)
        return None if order is None else order.snapshot

    def reconcile(self) -> ReconciliationSnapshot:
        return ReconciliationSnapshot(
            self.now,
            self._health.session_id or "fixture-disconnected",
            tuple(
                sorted(
                    (item.snapshot for item in self.orders.values()),
                    key=lambda item: item.client_order_id,
                )
            ),
            (),
            Decimal("0"),
            (),
            tuple(sorted(self._fills)),
        )

    def ingest_event(self, event: BrokerEvent, *, checksum_valid: bool = True) -> bool:
        """Apply an offline event once; gaps degrade health and stop new risk."""
        if not checksum_valid:
            self.kill_new_risk = True
            raise CorruptedBrokerPayload("offline fixture payload checksum mismatch")
        if event.business_identity in self._events:
            return False
        expected = self._last_sequence.get(event.client_order_id, 0) + 1
        if event.sequence != expected:
            self.kill_new_risk = True
            self._health = BrokerSessionHealth(
                SessionHealthState.DEGRADED,
                self.now,
                self._health.session_id,
                True,
                (f"sequence_gap:{expected}:{event.sequence}",),
            )
            return False
        order = self.orders.get(event.client_order_id)
        if order is None:
            self.kill_new_risk = True
            return False
        snapshot = order.snapshot
        if isinstance(event, BrokerFillEvent):
            if event.fill_id in self._fills:
                return False
            filled = snapshot.filled_quantity + event.quantity
            if filled > snapshot.quantity:
                self.kill_new_risk = True
                raise CorruptedBrokerPayload("fixture fill exceeds order quantity")
            state = (
                BrokerOrderState.FILLED
                if filled == snapshot.quantity
                else BrokerOrderState.PARTIALLY_FILLED
            )
            snapshot = replace(
                snapshot,
                filled_quantity=filled,
                state=state,
                last_sequence=event.sequence,
            )
            self._fills[event.fill_id] = event
        else:
            snapshot = replace(snapshot, state=event.state, last_sequence=event.sequence)
        self.orders[event.client_order_id] = FixtureOrder(order.request, snapshot)
        self._events[event.business_identity] = event
        self._last_sequence[event.client_order_id] = event.sequence
        return True

    def force_kill_new_risk(self, reason: str) -> None:
        self.kill_new_risk = True
        self._health = BrokerSessionHealth(
            SessionHealthState.DEGRADED,
            self.now,
            self._health.session_id,
            True,
            (reason,),
        )

    def clear_faults_and_reconnect(self) -> BrokerSessionHealth:
        self.faults = frozenset()
        self.kill_new_risk = False
        self._health = BrokerSessionHealth(
            SessionHealthState.HEALTHY, self.now, "fixture-session-reconnected", False
        )
        return self._health

    def _accepted(self, snapshot: BrokerOrderSnapshot) -> BrokerCommandResult:
        return BrokerCommandResult(
            CommandOutcome.ACCEPTED,
            snapshot.client_order_id,
            self.now,
            snapshot.broker_order_id,
        )

    def _rejected(
        self,
        client_order_id: str,
        code: str,
        category: BrokerErrorCategory = BrokerErrorCategory.BROKER_REJECTED,
        retry: RetryDirective = RetryDirective.NEVER,
    ) -> BrokerCommandResult:
        return BrokerCommandResult(
            CommandOutcome.REJECTED,
            client_order_id,
            self.now,
            error=BrokerError(1, category, code, f"offline fixture: {code}", retry),
        )
