"""FinMind historical price adapter for multi-asset research workloads."""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from datetime import date
from pathlib import Path

from ..data import DataError
from ..domain import Bar
from ..http import HttpClientError, JsonHttpClient


class FinMindHistoricalProvider:
    name = "finmind_taiwan_stock_price"
    endpoint = "https://api.finmindtrade.com/api/v4/data"

    def __init__(self, token: str | None = None, cache_dir: Path | None = None, http_client: JsonHttpClient | None = None):
        self.token = token if token is not None else os.getenv("FINMIND_TOKEN", "")
        self.cache_dir = cache_dir or Path(__file__).resolve().parents[2] / "data" / "cache"
        self.http_client = http_client or JsonHttpClient()

    def fetch_daily(self, symbol: str, start: str, end: str) -> list[Bar]:
        if not symbol.isdigit() or not 4 <= len(symbol) <= 6:
            raise DataError("股票代號須為 4～6 位數字")
        try:
            start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError as exc:
            raise DataError("日期格式須為 YYYY-MM-DD") from exc
        if start_date > end_date:
            raise DataError("開始日期不可晚於結束日期")

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self.cache_dir / f"finmind_{symbol}_{start}_{end}.json"
        is_live_range = end_date >= date.today()
        fresh = cache_path.exists() and (not is_live_range or time.time() - cache_path.stat().st_mtime < 900)
        payload = None
        if fresh:
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
        if payload is None:
            params = {
                "dataset": "TaiwanStockPrice",
                "data_id": symbol,
                "start_date": start,
                "end_date": end,
            }
            headers = {"Accept": "application/json", "User-Agent": "tw-stock-tool/2.0"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            try:
                payload = self.http_client.get(f"{self.endpoint}?{urllib.parse.urlencode(params)}", headers=headers, timeout=30)
            except HttpClientError as exc:
                raise DataError(f"研究資料 API 無法取得 {symbol}：{exc}") from exc
            if payload.get("status") != 200:
                raise DataError(f"研究資料 API 錯誤：{payload.get('msg', 'unknown error')}")
            cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        bars = []
        for row in payload.get("data", []):
            try:
                bars.append(Bar(
                    date=str(row["date"]), open=float(row["open"]), high=float(row["max"]),
                    low=float(row["min"]), close=float(row["close"]), volume=int(row["Trading_Volume"]),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        bars.sort(key=lambda item: item.date)
        if not bars:
            raise DataError(f"研究資料 API 查無 {symbol} 日 K")
        return bars
