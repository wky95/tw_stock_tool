from __future__ import annotations

import json
import os
import urllib.error
from unittest.mock import patch

import pytest

from island_quant.data.adapters.finmind import FinMindProvider
from island_quant.data.ports import DataRequest


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_finmind_rate_limit_honors_retry_after() -> None:
    rate_limit = urllib.error.HTTPError(
        "https://example.invalid",
        429,
        "rate limited",
        {"Retry-After": "3"},
        None,
    )
    success = FakeResponse(b'{"status":200,"data":[]}')
    provider = FinMindProvider(attempts=2, base_delay_seconds=0.01)
    with (
        patch("urllib.request.urlopen", side_effect=[rate_limit, success]),
        patch("island_quant.data.adapters.finmind.time.sleep") as sleep,
    ):
        payload = provider.fetch(DataRequest("TaiwanStockTradingDate"))

    assert payload.rows() == []
    sleep.assert_called_once_with(3.0)


@pytest.mark.network
@pytest.mark.skipif(
    os.getenv("RUN_NETWORK_TESTS") != "1", reason="explicit network opt-in required"
)
def test_finmind_contract_smoke() -> None:
    payload = FinMindProvider(token=os.getenv("FINMIND_TOKEN")).fetch(
        DataRequest("TaiwanStockPrice", "2330", "2024-01-02", "2024-01-03")
    )
    decoded = json.loads(payload.raw_body)
    assert decoded["status"] == 200
    assert decoded["data"][0]["stock_id"] == "2330"
