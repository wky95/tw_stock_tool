"""Domain models shared by backtesting, monitoring and integrations.

This module deliberately contains no network, storage or web concerns.  External
providers translate their payloads into these stable objects.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int

    def __post_init__(self):
        prices = (self.open, self.high, self.low, self.close)
        if any(value <= 0 for value in prices):
            raise ValueError("OHLC 價格必須大於 0")
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close, self.high):
            raise ValueError("日 K 高低價與開收盤價不一致")
        if self.volume < 0:
            raise ValueError("成交量不可為負數")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Instrument:
    symbol: str
    market: str = "tse"

    @property
    def key(self) -> str:
        return f"{self.market}:{self.symbol}"

    @classmethod
    def parse(cls, value: str | dict[str, str]) -> "Instrument":
        if isinstance(value, dict):
            symbol = str(value.get("symbol", "")).strip()
            market = str(value.get("market", "tse")).strip().lower()
        else:
            raw = str(value).strip().lower()
            if ":" in raw:
                first, second = raw.split(":", 1)
                if first in {"tse", "otc"}:
                    market, symbol = first, second
                else:
                    symbol, market = first, second
            else:
                symbol, market = raw, "tse"
        if not symbol.isdigit() or not 4 <= len(symbol) <= 6:
            raise ValueError(f"無效股票代號：{symbol or '(空白)'}")
        if market not in {"tse", "otc"}:
            raise ValueError(f"無效市場：{market}（僅支援 tse / otc）")
        return cls(symbol=symbol, market=market)


@dataclass(frozen=True)
class Quote:
    instrument: Instrument
    name: str
    timestamp: str
    price: float | None
    previous_close: float | None
    open: float | None
    high: float | None
    low: float | None
    volume: int | None
    bids: list[dict[str, float | int]] = field(default_factory=list)
    asks: list[dict[str, float | int]] = field(default_factory=list)
    status: str = "ok"

    @property
    def change(self) -> float | None:
        if self.price is None or self.previous_close is None:
            return None
        return self.price - self.previous_close

    @property
    def change_pct(self) -> float | None:
        if self.change is None or not self.previous_close:
            return None
        return self.change / self.previous_close * 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.instrument.symbol,
            "market": self.instrument.market,
            "key": self.instrument.key,
            "name": self.name,
            "timestamp": self.timestamp,
            "price": self.price,
            "previous_close": self.previous_close,
            "change": self.change,
            "change_pct": self.change_pct,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "volume": self.volume,
            "bids": self.bids,
            "asks": self.asks,
            "status": self.status,
        }


@dataclass(frozen=True)
class AlertRule:
    id: str
    instrument: Instrument
    condition: str
    threshold: float
    enabled: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AlertRule":
        condition = str(raw.get("condition", "above"))
        if condition not in {"above", "below", "change_pct_above", "change_pct_below"}:
            raise ValueError(f"不支援的警示條件：{condition}")
        threshold = float(raw.get("threshold"))
        return cls(
            id=str(raw.get("id") or "rule"),
            instrument=Instrument.parse(raw.get("instrument") or raw.get("symbol") or ""),
            condition=condition,
            threshold=threshold,
            enabled=bool(raw.get("enabled", True)),
        )


@dataclass(frozen=True)
class AlertEvent:
    rule_id: str
    instrument_key: str
    message: str
    observed_value: float
    threshold: float
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
