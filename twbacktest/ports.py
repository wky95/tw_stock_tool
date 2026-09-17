"""Integration contracts (ports).

Implement these protocols in adapters when adding a licensed quote vendor,
broker, database or notification channel.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .domain import AlertEvent, Bar, Instrument, Quote


@runtime_checkable
class HistoricalDataProvider(Protocol):
    name: str

    def fetch_daily(self, symbol: str, start: str, end: str) -> list[Bar]: ...


@runtime_checkable
class QuoteProvider(Protocol):
    name: str
    minimum_interval_seconds: int

    def fetch_quotes(self, instruments: list[Instrument]) -> list[Quote]: ...


@runtime_checkable
class Broker(Protocol):
    name: str

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]: ...
    def cancel_order(self, order_id: str) -> dict[str, Any]: ...
    def positions(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class Notifier(Protocol):
    name: str

    def send(self, event: AlertEvent) -> None: ...
