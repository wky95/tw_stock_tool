from datetime import UTC, datetime, timedelta

import pytest

from island_quant.domain.events import DomainEvent, EventKind


def test_event_rejects_recording_before_occurrence() -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    with pytest.raises(ValueError, match="recorded_at"):
        DomainEvent(
            kind=EventKind.BAR_AVAILABLE,
            occurred_at=now,
            recorded_at=now - timedelta(seconds=1),
            aggregate_id="TWSE:2330",
            payload={},
            sequence=1,
        )


def test_event_has_replay_metadata() -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    event = DomainEvent(
        kind=EventKind.SIGNAL_GENERATED,
        occurred_at=now,
        recorded_at=now,
        aggregate_id="strategy:baseline",
        payload={"value": "0.5"},
        sequence=7,
    )

    assert event.sequence == 7
    assert event.schema_version == 1
    assert event.event_id != event.correlation_id

