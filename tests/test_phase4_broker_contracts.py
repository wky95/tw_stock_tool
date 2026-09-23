from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from island_quant.brokers.contracts import (
    BrokerCapability,
    BrokerFillEvent,
    BrokerOrderEvent,
    BrokerOrderState,
    CancelOrderRequest,
    CommandOutcome,
    CredentialReference,
    OrderLot,
    ReplaceOrderRequest,
    RetryDirective,
    SessionHealthState,
    SubmitOrderRequest,
    compare_reconciliation,
)
from island_quant.brokers.fixture import (
    CorruptedBrokerPayload,
    FixtureFault,
    OfflineBrokerFixture,
)
from island_quant.domain.models import OrderType, Side, TimeInForce

NOW = datetime(2025, 1, 2, 9, 0, tzinfo=UTC)
CREDENTIAL = CredentialReference("offline_fixture", "test-reference", "test-account")


def request(
    client_order_id: str = "client-1",
    *,
    idempotency_key: str = "idem-1",
    order_lot: OrderLot = OrderLot.COMMON,
) -> SubmitOrderRequest:
    return SubmitOrderRequest(
        client_order_id,
        idempotency_key,
        "corr-1",
        "cause-1",
        "TWSE:2330",
        Side.BUY,
        1000,
        OrderType.LIMIT,
        TimeInForce.DAY,
        order_lot,
        NOW,
        Decimal("100"),
    )


def opened(**kwargs: object) -> OfflineBrokerFixture:
    fixture = OfflineBrokerFixture(now=NOW, **kwargs)  # type: ignore[arg-type]
    fixture.open(CREDENTIAL)
    return fixture


def fill_event(
    *,
    sequence: int = 2,
    quantity: int = 500,
    fill_id: str = "fill-1",
    business_identity: str = "fill-business-1",
) -> BrokerFillEvent:
    return BrokerFillEvent(
        1,
        f"event-{fill_id}",
        "corr-1",
        "cause-1",
        sequence,
        business_identity,
        fill_id,
        "client-1",
        "broker-id",
        "TWSE:2330",
        Side.BUY,
        quantity,
        Decimal("100"),
        NOW,
        NOW,
    )


def test_duplicate_submit_returns_same_broker_identity() -> None:
    fixture = opened()
    first = fixture.submit(request())
    second = fixture.submit(request())
    assert first == second
    assert len(fixture.orders) == 1


def test_duplicate_submit_with_changed_business_payload_fails_closed() -> None:
    fixture = opened()
    fixture.submit(request())
    result = fixture.submit(replace(request(), quantity=999))
    assert result.outcome is CommandOutcome.REJECTED
    assert result.error is not None and result.error.code == "IDEMPOTENCY_CONFLICT"


def test_lost_response_is_unknown_and_requires_reconciliation_not_resubmit() -> None:
    fixture = opened(faults=frozenset({FixtureFault.LOST_RESPONSE}))
    result = fixture.submit(request())
    assert result.outcome is CommandOutcome.UNKNOWN
    assert result.error is not None
    assert result.error.retry is RetryDirective.RECONCILE_THEN_DECIDE
    assert fixture.query_order("client-1") is not None


def test_out_of_order_callback_sets_sequence_gap_and_kills_new_risk() -> None:
    fixture = opened()
    fixture.submit(request())
    assert not fixture.ingest_event(fill_event(sequence=3))
    assert fixture.health().state is SessionHealthState.DEGRADED
    assert fixture.health().kill_new_risk


def test_duplicate_fill_is_idempotent() -> None:
    fixture = opened()
    fixture.submit(request())
    event = fill_event()
    assert fixture.ingest_event(event)
    assert not fixture.ingest_event(event)
    assert fixture.query_order("client-1").filled_quantity == 500  # type: ignore[union-attr]


def test_partial_fill_then_final_fill() -> None:
    fixture = opened()
    fixture.submit(request())
    fixture.ingest_event(fill_event())
    partial = fixture.query_order("client-1")
    assert partial is not None and partial.state is BrokerOrderState.PARTIALLY_FILLED
    fixture.ingest_event(
        fill_event(sequence=3, fill_id="fill-2", business_identity="fill-business-2")
    )
    final = fixture.query_order("client-1")
    assert final is not None and final.state is BrokerOrderState.FILLED


def test_cancel_fill_race_preserves_fill_and_rejects_late_cancel() -> None:
    fixture = opened()
    fixture.submit(request())
    fixture.ingest_event(fill_event(quantity=1000))
    result = fixture.cancel(CancelOrderRequest("client-1", "corr-1", "cause-2", NOW))
    assert result.outcome is CommandOutcome.REJECTED
    assert result.error is not None and result.error.code == "ALREADY_FILLED"


def test_replace_fill_race_cannot_reduce_below_filled_quantity() -> None:
    fixture = opened()
    fixture.submit(request())
    fixture.ingest_event(fill_event(quantity=500))
    result = fixture.replace(
        ReplaceOrderRequest("client-1", "corr-1", "cause-2", NOW, 400, Decimal("99"))
    )
    assert result.outcome is CommandOutcome.REJECTED
    assert result.error is not None and result.error.code == "QUANTITY_BELOW_FILLED"


def test_unknown_order_query_and_commands_are_explicit() -> None:
    fixture = opened()
    assert fixture.query_order("missing") is None
    result = fixture.cancel(CancelOrderRequest("missing", "corr", "cause", NOW))
    assert result.error is not None and result.error.code == "ORDER_NOT_FOUND"


def test_session_expiry_fails_closed() -> None:
    fixture = opened(faults=frozenset({FixtureFault.SESSION_EXPIRED}))
    assert fixture.health().state is SessionHealthState.EXPIRED
    assert fixture.submit(request()).outcome is CommandOutcome.REJECTED


def test_reconnect_preserves_authoritative_fixture_state() -> None:
    fixture = opened()
    fixture.submit(request())
    fixture.close()
    health = fixture.clear_faults_and_reconnect()
    assert health.state is SessionHealthState.HEALTHY
    assert fixture.query_order("client-1") is not None


def test_rate_limit_has_typed_backoff_error() -> None:
    fixture = opened(faults=frozenset({FixtureFault.RATE_LIMITED}))
    result = fixture.submit(request())
    assert result.error is not None
    assert result.error.retry is RetryDirective.BACKOFF


def test_sequence_gap_does_not_advance_order_projection() -> None:
    fixture = opened()
    fixture.submit(request())
    fixture.ingest_event(fill_event(sequence=4))
    order = fixture.query_order("client-1")
    assert order is not None and order.last_sequence == 1 and order.filled_quantity == 0


def test_corrupted_payload_raises_and_kills_new_risk() -> None:
    fixture = opened()
    fixture.submit(request())
    with pytest.raises(CorruptedBrokerPayload):
        fixture.ingest_event(fill_event(), checksum_valid=False)
    assert fixture.kill_new_risk


def test_unsupported_capability_is_rejected_before_side_effect() -> None:
    supported = frozenset({BrokerCapability.QUERY_ORDER})
    fixture = opened(supported=supported)
    result = fixture.submit(request())
    assert result.error is not None and result.error.code == "UNSUPPORTED_CAPABILITY"
    assert not fixture.orders


def test_odd_lot_rejection_is_fixture_policy_not_vendor_claim() -> None:
    fixture = opened(faults=frozenset({FixtureFault.ODD_LOT_REJECTED}))
    result = fixture.submit(request(order_lot=OrderLot.INTRADAY_ODD))
    assert result.error is not None and result.error.code == "ODD_LOT_REJECTED"


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        (FixtureFault.SUSPENDED, "INSTRUMENT_SUSPENDED"),
        (FixtureFault.LIMIT_LOCKED, "PRICE_LIMIT_LOCKED"),
    ],
)
def test_suspended_and_limit_locked_instruments_are_rejected(
    fault: FixtureFault, code: str
) -> None:
    fixture = opened(faults=frozenset({fault}))
    result = fixture.submit(request())
    assert result.error is not None and result.error.code == code


def test_reconciliation_mismatch_kills_new_risk() -> None:
    fixture = opened()
    fixture.submit(request())
    actual = fixture.reconcile()
    expected = replace(actual, settled_cash=Decimal("1"))
    comparison = compare_reconciliation(expected, actual)
    assert comparison.mismatches == ("settled_cash",)
    assert comparison.kill_new_risk


def test_manual_kill_new_risk_blocks_submit() -> None:
    fixture = opened()
    fixture.force_kill_new_risk("operator")
    assert fixture.submit(request()).outcome is CommandOutcome.REJECTED


def test_event_contract_rejects_naive_times_and_preserves_trace_identity() -> None:
    event = BrokerOrderEvent(
        1,
        "event-1",
        "corr-1",
        "cause-1",
        2,
        "business-1",
        "client-1",
        "broker-1",
        BrokerOrderState.ACKNOWLEDGED,
        "OK",
        NOW,
        NOW + timedelta(seconds=1),
    )
    assert event.correlation_id == "corr-1" and event.sequence == 2
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(event, received_time=datetime(2025, 1, 2, 9, 0))


def test_credential_contract_has_no_secret_value_field() -> None:
    assert set(CredentialReference.__dataclass_fields__) == {
        "provider",
        "reference_name",
        "account_alias",
    }


def test_machine_readable_readiness_report_is_closed_and_uses_vocabulary() -> None:
    path = Path("docs/readiness/phase4a_broker_readiness.json")
    report = json.loads(path.read_text(encoding="utf-8"))
    vocabulary = set(report["classification_vocabulary"])
    assert report["schema_version"] == 1
    assert report["live_trading_ready"] is False
    assert report["broker_adapter_implemented"] is False
    assert report["items"]
    assert all(item["status"] in vocabulary for item in report["items"])
    assert all(source["retrieved_on"] == "2026-09-24" for source in report["official_sources"])
