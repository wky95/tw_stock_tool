"""Factories and safe placeholders for external integrations."""
from __future__ import annotations

import os
from typing import Any

from .ports import Broker, HistoricalDataProvider, QuoteProvider
from .providers import FinMindHistoricalProvider, MisQuoteProvider


class IntegrationNotConfigured(RuntimeError):
    pass


class DisabledBroker:
    """Default broker adapter: explicit failure prevents accidental live orders."""

    name = "disabled"

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        raise IntegrationNotConfigured("尚未設定券商 API；不會送出任何委託")

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        raise IntegrationNotConfigured("尚未設定券商 API")

    def positions(self) -> list[dict[str, Any]]:
        return []


def build_quote_provider() -> QuoteProvider:
    provider = os.getenv("TW_STOCK_QUOTE_PROVIDER", "mis").lower()
    factories = {"mis": MisQuoteProvider}
    if provider not in factories:
        raise IntegrationNotConfigured(f"未知行情 provider：{provider}")
    return factories[provider]()


def build_broker() -> Broker:
    # Register a licensed broker adapter here, then select it by environment.
    provider = os.getenv("TW_STOCK_BROKER", "disabled").lower()
    if provider == "disabled":
        return DisabledBroker()
    raise IntegrationNotConfigured(f"未知券商 adapter：{provider}")


def build_research_provider() -> HistoricalDataProvider:
    provider = os.getenv("TW_STOCK_RESEARCH_PROVIDER", "finmind").lower()
    factories = {"finmind": FinMindHistoricalProvider}
    if provider not in factories:
        raise IntegrationNotConfigured(f"未知研究資料 provider：{provider}")
    return factories[provider]()
