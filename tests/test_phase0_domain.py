from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from island_quant.domain.models import (
    Bar,
    Instrument,
    Market,
    OrderIntent,
    OrderType,
    Side,
)

NOW = datetime(2026, 1, 2, 8, tzinfo=UTC)
INSTRUMENT = Instrument("2330", Market.TWSE)


def test_bar_enforces_point_in_time_ordering() -> None:
    with pytest.raises(ValueError, match="timestamps"):
        Bar(
            instrument=INSTRUMENT,
            event_time=NOW,
            available_time=NOW - timedelta(seconds=1),
            ingestion_time=NOW,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=1000,
            source="fixture",
            data_version="sha256:test",
        )


def test_domain_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Bar(
            instrument=INSTRUMENT,
            event_time=datetime(2026, 1, 2),
            available_time=NOW,
            ingestion_time=NOW,
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=1000,
            source="fixture",
            data_version="v1",
        )


def test_limit_order_requires_price_and_idempotency_key() -> None:
    with pytest.raises(ValueError, match="limit_price"):
        OrderIntent(
            portfolio_id="main",
            strategy_id="baseline",
            instrument=INSTRUMENT,
            side=Side.BUY,
            quantity=1000,
            order_type=OrderType.LIMIT,
            created_at=NOW,
            idempotency_key="rebalance-20260102-2330",
        )
