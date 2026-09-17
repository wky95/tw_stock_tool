from __future__ import annotations

import csv
import io
import json
import time
import urllib.parse
from datetime import date, datetime
from pathlib import Path

from .domain import Bar
from .http import HttpClientError, JsonHttpClient, RateLimiter


TWSE_ENDPOINT = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
TPEX_ENDPOINT = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
HTTP = JsonHttpClient()
TWSE_RATE_LIMITER = RateLimiter(0.8)


class DataError(ValueError):
    pass


def _months_between(start: date, end: date):
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        month += 1
        if month == 13:
            year += 1
            month = 1


def _number(value: str) -> float:
    clean = value.replace(",", "").strip()
    if clean in {"", "--", "---", "X0.00"}:
        raise ValueError("missing price")
    clean = clean.lstrip("X")
    return float(clean)


def _parse_roc_date(value: str) -> str:
    roc_year, month, day = (int(part) for part in value.split("/"))
    return f"{roc_year + 1911:04d}-{month:02d}-{day:02d}"


def _fetch_month(symbol: str, year: int, month: int) -> list[Bar]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{symbol}_{year:04d}{month:02d}.json"
    payload = None
    today = date.today()
    is_current_month = (year, month) == (today.year, today.month)
    if cache_path.exists() and not is_current_month:
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None

    if payload is None:
        query = urllib.parse.urlencode(
            {"date": f"{year:04d}{month:02d}01", "stockNo": symbol, "response": "json"}
        )
        last_error = None
        request_urls = (
            f"{TWSE_ENDPOINT}?{query}",
            f"{TWSE_ENDPOINT}?{query}&_={int(time.time() * 1000)}",
        )
        for attempt, request_url in enumerate(request_urls):
            try:
                with TWSE_RATE_LIMITER:
                    payload = HTTP.get(
                        request_url,
                        headers={
                            "User-Agent": "Mozilla/5.0 (compatible; tw-stock-tool/2.0)",
                            "Accept": "application/json",
                            "Accept-Language": "zh-TW,zh;q=0.9",
                            "Cache-Control": "no-cache" if attempt else "max-age=0",
                        },
                    )
                break
            except HttpClientError as exc:
                last_error = exc
        if payload is None:
            raise DataError(f"無法向證交所取得 {year}-{month:02d} 資料：{last_error}") from last_error

        if payload.get("stat") == "OK" and not is_current_month:
            cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    if payload.get("stat") != "OK":
        return []

    bars: list[Bar] = []
    for row in payload.get("data", []):
        try:
            bars.append(
                Bar(
                    date=_parse_roc_date(row[0]),
                    volume=int(_number(row[1])),
                    open=_number(row[3]),
                    high=_number(row[4]),
                    low=_number(row[5]),
                    close=_number(row[6]),
                )
            )
        except (ValueError, IndexError):
            continue
    return bars


def fetch_twse_daily(symbol: str, start: str, end: str) -> list[Bar]:
    symbol = symbol.strip()
    if not symbol.isdigit() or not (4 <= len(symbol) <= 6):
        raise DataError("股票代號須為 4～6 位數字（目前下載功能支援上市股票）")
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError as exc:
        raise DataError("日期格式須為 YYYY-MM-DD") from exc
    if start_date > end_date:
        raise DataError("開始日期不可晚於結束日期")
    if (end_date.year - start_date.year) * 12 + end_date.month - start_date.month > 240:
        raise DataError("單次下載期間不可超過 20 年")

    bars: list[Bar] = []
    months = list(_months_between(start_date, end_date))
    for year, month in months:
        bars.extend(_fetch_month(symbol, year, month))
    selected = [bar for bar in bars if start <= bar.date <= end]
    selected.sort(key=lambda bar: bar.date)
    if not selected:
        raise DataError("查無日 K 資料；請確認代號、日期，或改用 CSV 匯入")
    return selected


class TwseHistoricalProvider:
    """Historical data adapter kept behind the provider contract."""

    name = "twse_official_daily"

    def fetch_daily(self, symbol: str, start: str, end: str) -> list[Bar]:
        return fetch_twse_daily(symbol, start, end)


def _fetch_tpex_month(symbol: str, year: int, month: int) -> list[Bar]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"otc_{symbol}_{year:04d}{month:02d}.json"
    today = date.today()
    is_current_month = (year, month) == (today.year, today.month)
    payload = None
    if cache_path.exists() and not is_current_month:
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
    if payload is None:
        query = urllib.parse.urlencode({"code": symbol, "date": f"{year:04d}/{month:02d}/01", "response": "json"})
        try:
            payload = HTTP.get(
                f"{TPEX_ENDPOINT}?{query}",
                headers={"User-Agent": "tw-stock-tool/2.0", "Accept": "application/json"},
            )
        except HttpClientError as exc:
            raise DataError(f"無法向櫃買中心取得 {year}-{month:02d} 資料：{exc}") from exc
        if payload.get("stat") == "ok" and not is_current_month:
            cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if payload.get("stat") != "ok" or not payload.get("tables"):
        return []
    bars = []
    for row in payload["tables"][0].get("data", []):
        try:
            bars.append(Bar(
                date=_parse_roc_date(row[0]),
                volume=int(_number(row[1]) * 1000),  # TPEX reports 成交張數.
                open=_number(row[3]), high=_number(row[4]), low=_number(row[5]), close=_number(row[6]),
            ))
        except (ValueError, IndexError):
            continue
    return bars


def fetch_tpex_daily(symbol: str, start: str, end: str) -> list[Bar]:
    symbol = symbol.strip()
    if not symbol.isdigit() or not 4 <= len(symbol) <= 6:
        raise DataError("上櫃股票代號須為 4～6 位數字")
    try:
        start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as exc:
        raise DataError("日期格式須為 YYYY-MM-DD") from exc
    if start_date > end_date:
        raise DataError("開始日期不可晚於結束日期")
    if (end_date.year - start_date.year) * 12 + end_date.month - start_date.month > 240:
        raise DataError("單次下載期間不可超過 20 年")
    bars = []
    months = list(_months_between(start_date, end_date))
    for index, (year, month) in enumerate(months):
        bars.extend(_fetch_tpex_month(symbol, year, month))
        if index < len(months) - 1:
            time.sleep(0.06)
    selected = sorted((bar for bar in bars if start <= bar.date <= end), key=lambda bar: bar.date)
    if not selected:
        raise DataError("查無上櫃日 K 資料；請確認代號與日期")
    return selected


class TpexHistoricalProvider:
    name = "tpex_official_daily"

    def fetch_daily(self, symbol: str, start: str, end: str) -> list[Bar]:
        return fetch_tpex_daily(symbol, start, end)


def parse_csv_text(text: str) -> list[Bar]:
    text = text.lstrip("\ufeff")
    if not text.strip():
        raise DataError("CSV 內容是空的")
    reader = csv.DictReader(io.StringIO(text))
    aliases = {
        "date": ("date", "日期", "Date"),
        "open": ("open", "開盤價", "Open"),
        "high": ("high", "最高價", "High"),
        "low": ("low", "最低價", "Low"),
        "close": ("close", "收盤價", "Close"),
        "volume": ("volume", "成交股數", "Volume"),
    }
    fields = reader.fieldnames or []
    mapping: dict[str, str] = {}
    for key, choices in aliases.items():
        found = next((choice for choice in choices if choice in fields), None)
        if not found:
            raise DataError(f"CSV 缺少欄位：{key}")
        mapping[key] = found

    bars: list[Bar] = []
    for line_number, row in enumerate(reader, start=2):
        try:
            raw_date = row[mapping["date"]].strip()
            if "/" in raw_date:
                parts = raw_date.split("/")
                if len(parts[0]) <= 3:
                    raw_date = _parse_roc_date(raw_date)
                else:
                    raw_date = datetime.strptime(raw_date, "%Y/%m/%d").date().isoformat()
            else:
                raw_date = date.fromisoformat(raw_date).isoformat()
            bar = Bar(
                date=raw_date,
                open=_number(row[mapping["open"]]),
                high=_number(row[mapping["high"]]),
                low=_number(row[mapping["low"]]),
                close=_number(row[mapping["close"]]),
                volume=int(_number(row[mapping["volume"]])),
            )
            if min(bar.open, bar.high, bar.low, bar.close) <= 0:
                raise ValueError("price must be positive")
            bars.append(bar)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise DataError(f"CSV 第 {line_number} 行格式錯誤：{exc}") from exc

    unique = {bar.date: bar for bar in bars}
    result = sorted(unique.values(), key=lambda bar: bar.date)
    if len(result) < 2:
        raise DataError("至少需要 2 筆日 K 資料")
    return result
