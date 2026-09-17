import unittest

from twbacktest.api import ApiController
from twbacktest.domain import Instrument, Quote
from twbacktest.integrations import DisabledBroker
from twbacktest.monitoring import MonitoringService
from twbacktest.providers.mis import MisQuoteProvider


class FakeQuoteProvider:
    name = "fake_quotes"
    minimum_interval_seconds = 1

    def fetch_quotes(self, instruments):
        return [Quote(
            instrument=item,
            name="測試股票",
            timestamp="2026-01-02T10:00:00+08:00",
            price=110,
            previous_close=100,
            open=101,
            high=112,
            low=99,
            volume=1234,
        ) for item in instruments]


class FakeHistoricalProvider:
    name = "fake_history"

    def fetch_daily(self, symbol, start, end):
        raise AssertionError("not used in this test")


class FakeHttpClient:
    def get(self, url, headers=None, timeout=0):
        return {
            "rtcode": "0000",
            "msgArray": [{
                "c": "2330", "ex": "tse", "n": "台積電", "d": "20260102", "t": "10:00:00",
                "z": "101.0000", "y": "100.0000", "o": "99.0000", "h": "102.0000", "l": "98.0000",
                "v": "1234", "b": "100.5000_100.0000_", "g": "12_20_",
                "a": "101.0000_101.5000_", "f": "8_9_",
            }],
        }


class DomainTests(unittest.TestCase):
    def test_instrument_accepts_both_notations(self):
        self.assertEqual(Instrument.parse("6488:otc").key, "otc:6488")
        self.assertEqual(Instrument.parse("tse:2330").key, "tse:2330")

    def test_instrument_rejects_unknown_market(self):
        with self.assertRaises(ValueError):
            Instrument.parse("2330:unknown")

    def test_bar_rejects_inconsistent_ohlc(self):
        from twbacktest.domain import Bar
        with self.assertRaises(ValueError):
            Bar("2026-01-01", open=100, high=99, low=90, close=98, volume=1)


class MonitoringTests(unittest.TestCase):
    def test_mis_adapter_translates_vendor_payload(self):
        provider = MisQuoteProvider(http_client=FakeHttpClient())
        quote = provider.fetch_quotes([Instrument("2330", "tse")])[0]
        self.assertEqual(quote.price, 101)
        self.assertEqual(quote.change_pct, 1)
        self.assertEqual(quote.bids[0], {"price": 100.5, "volume": 12})

    def test_snapshot_evaluates_price_and_percent_alerts(self):
        service = MonitoringService(FakeQuoteProvider())
        result = service.snapshot(["2330:tse"], [
            {"id": "price", "instrument": "2330:tse", "condition": "above", "threshold": 109},
            {"id": "pct", "instrument": "2330:tse", "condition": "change_pct_above", "threshold": 9},
            {"id": "off", "instrument": "2330:tse", "condition": "below", "threshold": 90},
        ])
        self.assertEqual(len(result["quotes"]), 1)
        self.assertEqual({event["rule_id"] for event in result["alerts"]}, {"price", "pct"})

    def test_api_controller_is_dependency_injected(self):
        controller = ApiController(
            {"tse": FakeHistoricalProvider(), "otc": FakeHistoricalProvider()},
            FakeQuoteProvider(),
            DisabledBroker(),
        )
        response = controller.handle("POST", "/api/v1/monitor/snapshot", {}, {"instruments": ["2330:tse"]})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["result"]["quotes"][0]["price"], 110)

    def test_capabilities_expose_safe_broker_default(self):
        controller = ApiController(
            {"tse": FakeHistoricalProvider(), "otc": FakeHistoricalProvider()},
            FakeQuoteProvider(),
            DisabledBroker(),
        )
        response = controller.handle("GET", "/api/v1/capabilities", {}, None)
        self.assertFalse(response.payload["result"]["live_order_execution"])


if __name__ == "__main__":
    unittest.main()
