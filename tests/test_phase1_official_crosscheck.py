from __future__ import annotations

import json
import os

import pytest

from island_quant.data.adapters.finmind import FinMindProvider
from island_quant.data.cross_validation import compare_volume_shares
from island_quant.data.ports import DataRequest
from twbacktest.data import TpexHistoricalProvider, TwseHistoricalProvider


@pytest.mark.network
@pytest.mark.skipif(
    os.getenv("RUN_NETWORK_TESTS") != "1", reason="explicit network opt-in required"
)
@pytest.mark.parametrize(
    ("symbol", "official", "volume_tolerance"),
    [
        ("2330", TwseHistoricalProvider(), 0),
        ("6488", TpexHistoricalProvider(), 999),
    ],
)
def test_finmind_sample_matches_official_daily_data(symbol, official, volume_tolerance) -> None:
    start, end = "2024-01-02", "2024-01-05"
    payload = FinMindProvider(token=os.getenv("FINMIND_TOKEN")).fetch(
        DataRequest("TaiwanStockPrice", symbol, start, end)
    )
    finmind = {row["date"]: row for row in json.loads(payload.raw_body)["data"]}
    official_rows = {bar.date: bar for bar in official.fetch_daily(symbol, start, end)}
    common_dates = sorted(finmind.keys() & official_rows.keys())
    assert len(common_dates) == 4
    for day in common_dates:
        row = finmind[day]
        bar = official_rows[day]
        assert (float(row["open"]), float(row["max"]), float(row["min"]), float(row["close"])) == (
            bar.open,
            bar.high,
            bar.low,
            bar.close,
        )
        comparison = compare_volume_shares(
            normalized_shares=int(row["Trading_Volume"]),
            reference_raw_value=bar.volume / 1000 if volume_tolerance else bar.volume,
            reference_raw_unit="thousand_shares" if volume_tolerance else "shares",
        )
        assert comparison.absolute_difference_shares <= volume_tolerance
        assert comparison.relative_difference >= 0
