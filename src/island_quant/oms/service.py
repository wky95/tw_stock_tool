"""Persistent paper OMS application service and transactional outbox worker."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal

from island_quant.backtest.policies import FeeTaxPolicy
from island_quant.brokers.paper import DeterministicPaperBroker, PaperOrderRequest
from island_quant.domain.models import Side
from island_quant.oms.models import OMSEvent, OMSOrder, OMSState, PaperOrderLineage
from island_quant.oms.repository import SQLiteOMSRepository


@dataclass(frozen=True, slots=True)
class PaperOrderCommand:
    client_order_id: str
    idempotency_key: str
    instrument_id: str
    side: str
    quantity: int
    estimated_price: Decimal
    available_cash: Decimal
    created_at: datetime
    available_position: int = 0
    lineage: PaperOrderLineage | None = None


class PaperOMSService:
    def __init__(
        self,
        repository: SQLiteOMSRepository,
        fee_policy: FeeTaxPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.fee_policy = fee_policy or FeeTaxPolicy()

    def queue(self, command: PaperOrderCommand) -> OMSOrder:
        existing = self.repository.by_idempotency_key(command.idempotency_key)
        if existing is not None:
            return existing
        identity = (
            f"paper:{command.client_order_id}:{command.idempotency_key}:"
            f"{command.instrument_id}:{command.side}:{command.quantity}"
        )
        order_id = hashlib.sha256(identity.encode()).hexdigest()
        order = OMSOrder(
            order_id,
            command.client_order_id,
            command.idempotency_key,
            command.instrument_id,
            command.side,
            command.quantity,
            0,
            None,
            OMSState.CREATED,
            1,
            1,
            "paper",
            command.created_at,
            command.created_at,
        )
        lineage_payload = asdict(command.lineage) if command.lineage is not None else None
        if lineage_payload is not None and command.lineage is not None:
            lineage_payload["decision_time"] = command.lineage.decision_time.isoformat()
            lineage_payload["target_weight"] = str(command.lineage.target_weight)
        created = self._event(
            order,
            OMSState.CREATED,
            None,
            "command_accepted",
            "created",
            lineage_payload,
        )
        current = self.repository.create(order, created, lineage=command.lineage)
        current = self._transition(current, OMSState.RISK_PENDING, "risk_evaluation_started")
        gross = command.estimated_price * command.quantity
        estimated_fee, estimated_tax = self.fee_policy.costs(Side(command.side), gross)
        required = gross + estimated_fee + estimated_tax
        unreserved_cash = command.available_cash - self.repository.reserved_cash()
        if command.side == "buy" and required > unreserved_cash:
            return self._transition(current, OMSState.RISK_REJECTED, "insufficient_cash")
        unreserved_position = command.available_position - self.repository.reserved_sell_quantity(
            command.instrument_id
        )
        if command.side == "sell" and command.quantity > unreserved_position:
            return self._transition(current, OMSState.RISK_REJECTED, "insufficient_position")
        current = self._transition(current, OMSState.RISK_APPROVED, "paper_risk_approved")
        outbox_id = hashlib.sha256(f"submit:{command.idempotency_key}".encode()).hexdigest()
        payload = {
            "client_order_id": command.client_order_id,
            "idempotency_key": command.idempotency_key,
            "instrument_id": command.instrument_id,
            "side": command.side,
            "quantity": command.quantity,
        }
        event = self._event(
            current, OMSState.SUBMIT_PENDING, current.state, "paper_submit_queued", "submit_pending"
        )
        return self.repository.transition(
            current.order_id,
            event,
            expected_version=current.version,
            outbox=(outbox_id, "submit", payload),
            reservation=(required if command.side == "buy" else Decimal("0"), command.quantity),
        )

    def process_outbox(
        self,
        broker: DeterministicPaperBroker,
        *,
        crash_after_broker_success: bool = False,
    ) -> int:
        processed = 0
        for item in self.repository.pending_outbox():
            payload = json.loads(str(item["payload"]))
            request = PaperOrderRequest(
                str(payload["client_order_id"]),
                str(payload["idempotency_key"]),
                str(payload["instrument_id"]),
                str(payload["side"]),
                int(payload["quantity"]),
            )
            result = broker.submit(request)
            self.repository.increment_outbox_attempt(str(item["outbox_id"]))
            if crash_after_broker_success:
                raise RuntimeError("injected crash after paper broker success")
            order = self.repository.get(str(item["order_id"]))
            if order.state is OMSState.SUBMIT_PENDING:
                order = self._transition(order, OMSState.SUBMITTED, "paper_request_delivered")
            if result.status == "rejected":
                if order.state in {OMSState.SUBMITTED, OMSState.SUBMIT_PENDING}:
                    order = self._transition(
                        order,
                        OMSState.REJECTED,
                        result.reason_code,
                        release_reservation=True,
                    )
            else:
                if order.state is OMSState.SUBMITTED:
                    order = self._transition(order, OMSState.ACKNOWLEDGED, result.reason_code)
                for fill in result.fills:
                    following = (
                        OMSState.FILLED
                        if order.filled_quantity + fill.quantity == order.quantity
                        else OMSState.PARTIALLY_FILLED
                    )
                    event = self._event(
                        order,
                        following,
                        order.state,
                        "paper_fill",
                        f"fill:{fill.business_identity}",
                        {"fill_id": fill.fill_id, "quantity": fill.quantity, "price": fill.price},
                        event_time=fill.event_time,
                    )
                    order = self.repository.transition_fill(
                        order.order_id,
                        event,
                        expected_version=order.version,
                        fill_id=fill.fill_id,
                        business_identity=fill.business_identity,
                        quantity=fill.quantity,
                        price=fill.price,
                        event_time=fill.event_time,
                    )
            self.repository.mark_outbox_delivered(str(item["outbox_id"]), result.acknowledged_at)
            processed += 1
        return processed

    def request_cancel(self, order_id: str, at: datetime) -> OMSOrder:
        order = self.repository.get(order_id)
        event = self._event(
            order, OMSState.CANCEL_PENDING, order.state, "cancel_requested", "cancel", event_time=at
        )
        return self.repository.transition(order_id, event, expected_version=order.version)

    def apply_cancel_result(self, order_id: str, status: str, at: datetime) -> OMSOrder:
        order = self.repository.get(order_id)
        following = OMSState.CANCELLED if status == "cancelled" else OMSState.UNKNOWN
        event = self._event(
            order, following, order.state, status, f"cancel_result:{status}", event_time=at
        )
        return self.repository.transition(
            order_id,
            event,
            expected_version=order.version,
            release_reservation=following is OMSState.CANCELLED,
        )

    def request_replace(self, order_id: str, at: datetime) -> OMSOrder:
        order = self.repository.get(order_id)
        event = self._event(
            order,
            OMSState.REPLACE_PENDING,
            order.state,
            "replace_requested",
            "replace",
            event_time=at,
        )
        return self.repository.transition(order_id, event, expected_version=order.version)

    def apply_replace_result(self, order_id: str, status: str, at: datetime) -> OMSOrder:
        order = self.repository.get(order_id)
        following = OMSState.REPLACED if status == "replaced" else OMSState.UNKNOWN
        event = self._event(
            order, following, order.state, status, f"replace_result:{status}", event_time=at
        )
        return self.repository.transition(order_id, event, expected_version=order.version)

    def recover_unknown(
        self, order_id: str, state: OMSState, reason: str, at: datetime
    ) -> OMSOrder:
        order = self.repository.get(order_id)
        if order.state is not OMSState.UNKNOWN:
            raise ValueError("only Unknown orders may be manually recovered")
        event = self._event(
            order,
            state,
            order.state,
            reason,
            f"manual_recovery:{order.sequence + 1}",
            event_time=at,
        )
        return self.repository.transition(order_id, event, expected_version=order.version)

    def _transition(
        self,
        order: OMSOrder,
        state: OMSState,
        reason: str,
        *,
        release_reservation: bool = False,
    ) -> OMSOrder:
        event = self._event(order, state, order.state, reason, state.value)
        return self.repository.transition(
            order.order_id,
            event,
            expected_version=order.version,
            release_reservation=release_reservation,
        )

    @staticmethod
    def _event(
        order: OMSOrder,
        state: OMSState,
        previous: OMSState | None,
        reason: str,
        business: str,
        payload: dict[str, object] | None = None,
        event_time: datetime | None = None,
    ) -> OMSEvent:
        at = event_time or order.updated_at
        return OMSEvent.create(
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            idempotency_key=order.idempotency_key,
            correlation_id=order.order_id,
            causation_id=None if previous is None else f"{order.order_id}:{order.sequence}",
            expected_previous_state=previous,
            state=state,
            event_time=at,
            received_time=at,
            sequence=order.sequence if previous is None else order.sequence + 1,
            reason_code=reason,
            business_identity=f"{order.order_id}:{business}",
            payload=payload,
        )
