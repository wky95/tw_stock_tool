"""Normalization from provider rows into versioned, point-in-time schemas."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

MARKET_TIMEZONE = ZoneInfo("Asia/Taipei")
PRICE_DATASET = "daily_prices"
INSTRUMENT_DATASET = "instrument_master"
CALENDAR_DATASET = "trading_calendar"
UNIVERSE_DATASET = "universe_membership"
UNIVERSE_METADATA_DATASET = "universe_metadata"
QUALITY_DATASET = "data_quality_report"
CORPORATE_ACTION_DATASET = "corporate_actions"
CANONICAL_PRICE_DATASET = "canonical_daily_prices"
SPLIT_ADJUSTED_PRICE_DATASET = "split_adjusted_daily_prices"
TOTAL_RETURN_PRICE_DATASET = "total_return_daily_prices"
ADJUSTMENT_FACTOR_DATASET = "price_adjustment_factors"
LABEL_DATASET = "forward_return_labels"

_EXCLUDED_CATEGORIES = {"ETF", "ETN", "存託憑證", "受益證券", "權證"}


def _at(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=MARKET_TIMEZONE)


def _row_checksum(row: dict[str, Any]) -> str:
    encoded = json.dumps(
        row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _date(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc


def classify_security(stock_id: str, name: str, categories: Iterable[str]) -> str:
    category_set = set(categories)
    if category_set & _EXCLUDED_CATEGORIES:
        return "excluded_product"
    if not re.fullmatch(r"[0-9]{4}", stock_id):
        return "excluded_product"
    if "-DR" in name.upper() or name.endswith("特") or "特別股" in name:
        return "excluded_product"
    return "ordinary_share"


def normalize_prices(
    rows: list[dict[str, Any]], provider: str, ingested_at: datetime, schema_version: int
) -> pl.DataFrame:
    normalized: list[dict[str, Any]] = []
    for source in rows:
        trade_date = _date(source.get("date"), "price date")
        core: dict[str, Any] = {
            "instrument_id": str(source.get("stock_id", "")),
            "trade_date": trade_date,
            "event_time": _at(trade_date, time(13, 30)),
            "available_at": _at(trade_date, time(17, 30)),
            "ingested_at": ingested_at,
            "open": float(source.get("open", 0)),
            "high": float(source.get("max", source.get("high", 0))),
            "low": float(source.get("min", source.get("low", 0))),
            "close": float(source.get("close", 0)),
            "volume": int(source.get("Trading_Volume", source.get("volume", 0))),
            "volume_unit": "shares",
            "source_volume": int(source.get("Trading_Volume", source.get("volume", 0))),
            "source_volume_unit": "shares",
            "traded_value": float(source.get("Trading_money", source.get("traded_value", 0))),
            "turnover": int(source.get("Trading_turnover", source.get("turnover", 0))),
            "provider": provider,
            "dataset": "TaiwanStockPrice",
            "schema_version": schema_version,
        }
        core["checksum"] = _row_checksum(core)
        normalized.append(core)
    return pl.DataFrame(normalized, infer_schema_length=None)


def normalize_calendar(
    rows: list[dict[str, Any]], provider: str, ingested_at: datetime, schema_version: int
) -> pl.DataFrame:
    normalized = []
    for source in rows:
        trading_date = _date(source.get("date"), "trading date")
        core: dict[str, Any] = {
            "trade_date": trading_date,
            "event_time": _at(trading_date, time(0, 0)),
            "available_at": ingested_at,
            "ingested_at": ingested_at,
            "is_trading_day": True,
            "provider": provider,
            "dataset": "TaiwanStockTradingDate",
            "schema_version": schema_version,
        }
        core["checksum"] = _row_checksum(core)
        normalized.append(core)
    return pl.DataFrame(normalized, infer_schema_length=None)


def normalize_instruments(
    info_rows: list[dict[str, Any]],
    delisting_rows: list[dict[str, Any]],
    prices: pl.DataFrame,
    provider: str,
    ingested_at: datetime,
    schema_version: int,
) -> pl.DataFrame:
    grouped: dict[str, dict[str, Any]] = {}
    for row in info_rows:
        stock_id = str(row.get("stock_id", ""))
        entry = grouped.setdefault(
            stock_id,
            {
                "names": set(),
                "markets": set(),
                "categories": set(),
                "dates": [],
                "market_dates": [],
            },
        )
        entry["names"].add(str(row.get("stock_name", "")))
        entry["markets"].add(str(row.get("type", "")).lower())
        entry["categories"].add(str(row.get("industry_category", "")))
        market_date = _date(row.get("date"), "instrument available date")
        entry["dates"].append(market_date)
        entry["market_dates"].append((str(row.get("type", "")).lower(), market_date))

    delisted: dict[str, tuple[date, str]] = {}
    for row in delisting_rows:
        stock_id = str(row.get("stock_id", ""))
        delisted[stock_id] = (
            _date(row.get("date"), "delisting date"),
            str(row.get("stock_name", "")),
        )
        grouped.setdefault(
            stock_id,
            {
                "names": {str(row.get("stock_name", ""))},
                "markets": set(),
                "categories": set(),
                "dates": [],
                "market_dates": [],
            },
        )

    first_prices: dict[str, date] = {}
    price_dates: dict[str, list[date]] = {}
    if prices.height:
        for row in prices.group_by("instrument_id").agg(pl.col("trade_date").min()).to_dicts():
            first_prices[str(row["instrument_id"])] = row["trade_date"]
        for row in prices.select("instrument_id", "trade_date").sort("trade_date").to_dicts():
            price_dates.setdefault(str(row["instrument_id"]), []).append(row["trade_date"])

    normalized: list[dict[str, Any]] = []
    for stock_id, entry in grouped.items():
        markets = entry["markets"] & {"twse", "tpex"}
        market = next(iter(markets)) if len(markets) == 1 else "unknown"
        name = (
            sorted(entry["names"])[-1]
            if entry["names"]
            else delisted.get(stock_id, (None, ""))[1]
        )
        categories = sorted(entry["categories"])
        security_type = classify_security(stock_id, name, categories)
        emerging_dates = [
            market_date
            for market_type, market_date in entry["market_dates"]
            if market_type == "emerging"
        ]
        if emerging_dates and market in {"twse", "tpex"}:
            emerging_end = max(emerging_dates)
            listing_date = next(
                (day for day in price_dates.get(stock_id, []) if day > emerging_end), None
            )
            listing_date_source = "first_observed_price_after_emerging"
        else:
            listing_date = first_prices.get(stock_id)
            listing_date_source = "first_observed_price"
        delisting_date = delisted.get(stock_id, (None, ""))[0]
        available_date = max(entry["dates"]) if entry["dates"] else delisting_date
        core: dict[str, Any] = {
            "instrument_id": stock_id,
            "name": name,
            "market": market,
            "security_type": security_type,
            "industry_categories": json.dumps(categories, ensure_ascii=False),
            "listing_date": listing_date,
            "listing_date_source": listing_date_source,
            "listing_date_quality": "provisional",
            "observed_at": ingested_at,
            "delisting_date": delisting_date,
            "event_time": _at(available_date, time(0, 0)) if available_date else ingested_at,
            "available_at": ingested_at,
            "ingested_at": ingested_at,
            "provider": provider,
            "dataset": "TaiwanStockInfo+TaiwanStockDelisting",
            "schema_version": schema_version,
        }
        core["checksum"] = _row_checksum(core)
        normalized.append(core)
    return pl.DataFrame(normalized, infer_schema_length=None)
