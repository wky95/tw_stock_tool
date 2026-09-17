import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from twbacktest import data
from twbacktest.http import HttpClientError
from twbacktest.providers.finmind import FinMindHistoricalProvider


class FallbackHttpClient:
    def __init__(self):
        self.urls = []

    def get(self, url, headers=None, timeout=20):
        self.urls.append(url)
        if len(self.urls) == 1:
            raise HttpClientError("HTTP Error 308: redirect loop")
        return {
            "stat": "OK",
            "data": [["112/12/01", "1,000", "100,000", "100", "102", "99", "101", "+1", "10", ""]],
        }


class HistoricalProviderTests(unittest.TestCase):
    def test_twse_uses_cache_busting_retry_after_redirect_loop(self):
        client = FallbackHttpClient()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(data, "CACHE_DIR", Path(directory)), \
                 patch.object(data, "HTTP", client), \
                 patch.object(data, "TWSE_RATE_LIMITER", nullcontext()):
                bars = data._fetch_month("2330", 2023, 12)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].close, 101)
        self.assertEqual(len(client.urls), 2)
        self.assertIn("/rwd/zh/afterTrading/STOCK_DAY", client.urls[1])
        self.assertIn("&_=", client.urls[1])

    def test_finmind_adapter_maps_full_range_payload(self):
        class FakeFinMindHttp:
            def get(self, url, headers=None, timeout=20):
                self.headers = headers
                return {"status": 200, "data": [{
                    "date": "2026-01-02", "open": 100, "max": 103, "min": 99,
                    "close": 102, "Trading_Volume": 123456,
                }]}
        client = FakeFinMindHttp()
        with tempfile.TemporaryDirectory() as directory:
            provider = FinMindHistoricalProvider(token="secret", cache_dir=Path(directory), http_client=client)
            bars = provider.fetch_daily("2330", "2026-01-01", "2026-01-03")
        self.assertEqual(bars[0].close, 102)
        self.assertEqual(client.headers["Authorization"], "Bearer secret")


if __name__ == "__main__":
    unittest.main()
