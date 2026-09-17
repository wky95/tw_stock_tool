"""TWSE MIS five-second snapshot adapter.

The public MIS website is useful for local, personal monitoring.  It is not a
contracted market-data feed and has no availability guarantee.  Replace this
adapter through ``QuoteProvider`` for production or commercial use.
"""
from __future__ import annotations

import urllib.parse

from ..domain import Instrument, Quote
from ..http import HttpClientError, JsonHttpClient


class QuoteProviderError(RuntimeError):
    pass


def _float(value) -> float | None:
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def _levels(prices: str, volumes: str) -> list[dict[str, float | int]]:
    price_values = prices.split("_") if prices else []
    volume_values = volumes.split("_") if volumes else []
    result = []
    for price, volume in zip(price_values, volume_values):
        parsed_price = _float(price)
        parsed_volume = _int(volume)
        if parsed_price is not None:
            result.append({"price": parsed_price, "volume": parsed_volume or 0})
    return result[:5]


class MisQuoteProvider:
    name = "twse_mis_snapshot"
    minimum_interval_seconds = 5
    endpoint = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"

    def __init__(self, timeout_seconds: float = 10, http_client: JsonHttpClient | None = None):
        self.timeout_seconds = timeout_seconds
        self.http_client = http_client or JsonHttpClient()

    def fetch_quotes(self, instruments: list[Instrument]) -> list[Quote]:
        if not instruments:
            return []
        if len(instruments) > 50:
            raise QuoteProviderError("單次最多查詢 50 檔股票")
        channel = "|".join(f"{item.market}_{item.symbol}.tw" for item in instruments)
        query = urllib.parse.urlencode({"ex_ch": channel, "json": "1", "delay": "0"})
        try:
            payload = self.http_client.get(
                f"{self.endpoint}?{query}",
                headers={
                    "User-Agent": "tw-stock-tool/2.0",
                    "Accept": "application/json",
                    "Referer": "https://mis.twse.com.tw/stock/index.jsp",
                },
                timeout=self.timeout_seconds,
            )
        except HttpClientError as exc:
            raise QuoteProviderError(f"無法取得盤中行情：{exc}") from exc
        if payload.get("rtcode") != "0000":
            raise QuoteProviderError("盤中行情服務回傳錯誤")

        requested = {item.key: item for item in instruments}
        quotes = []
        for raw in payload.get("msgArray", []):
            instrument = Instrument(str(raw.get("c", "")), str(raw.get("ex", "tse")))
            last = _float(raw.get("z")) or _float(raw.get("pz")) or _float(raw.get("y"))
            date_value = str(raw.get("d", ""))
            time_value = str(raw.get("t", "") or raw.get("%", ""))
            timestamp = (
                f"{date_value[:4]}-{date_value[4:6]}-{date_value[6:8]}T{time_value}+08:00"
                if len(date_value) == 8 and time_value else ""
            )
            quotes.append(Quote(
                instrument=instrument,
                name=str(raw.get("n", instrument.symbol)),
                timestamp=timestamp,
                price=last,
                previous_close=_float(raw.get("y")),
                open=_float(raw.get("o")),
                high=_float(raw.get("h")),
                low=_float(raw.get("l")),
                volume=_int(raw.get("v")),
                bids=_levels(str(raw.get("b", "")), str(raw.get("g", ""))),
                asks=_levels(str(raw.get("a", "")), str(raw.get("f", ""))),
                status="ok" if _float(raw.get("z")) is not None else "reference",
            ))

        returned = {quote.instrument.key for quote in quotes}
        for key, instrument in requested.items():
            if key not in returned:
                quotes.append(Quote(instrument, instrument.symbol, "", None, None, None, None, None, None, status="not_found"))
        return quotes
