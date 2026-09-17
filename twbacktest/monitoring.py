from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .domain import AlertEvent, AlertRule, Instrument, Quote
from .ports import QuoteProvider


class MonitoringService:
    def __init__(self, quote_provider: QuoteProvider):
        self.quote_provider = quote_provider

    def snapshot(self, instruments_raw: list[Any], rules_raw: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        instruments = list(dict.fromkeys(Instrument.parse(item) for item in instruments_raw))
        if not instruments:
            raise ValueError("觀察清單不可為空")
        quotes = self.quote_provider.fetch_quotes(instruments)
        rules = [AlertRule.from_dict(raw) for raw in (rules_raw or [])]
        events = self._evaluate(quotes, rules)
        now = datetime.now(ZoneInfo("Asia/Taipei"))
        minutes = now.hour * 60 + now.minute
        session = "open" if now.weekday() < 5 and 9 * 60 <= minutes <= 13 * 60 + 30 else "closed"
        return {
            "provider": self.quote_provider.name,
            "minimum_interval_seconds": self.quote_provider.minimum_interval_seconds,
            "fetched_at": now.isoformat(timespec="seconds"),
            "market_session": session,
            "quotes": [quote.to_dict() for quote in quotes],
            "alerts": [event.to_dict() for event in events],
        }

    @staticmethod
    def _evaluate(quotes: list[Quote], rules: list[AlertRule]) -> list[AlertEvent]:
        by_key = {quote.instrument.key: quote for quote in quotes}
        events: list[AlertEvent] = []
        labels = {
            "above": "價格高於",
            "below": "價格低於",
            "change_pct_above": "漲跌幅高於",
            "change_pct_below": "漲跌幅低於",
        }
        for rule in rules:
            if not rule.enabled:
                continue
            quote = by_key.get(rule.instrument.key)
            if not quote:
                continue
            value = quote.change_pct if rule.condition.startswith("change_pct") else quote.price
            if value is None:
                continue
            triggered = value >= rule.threshold if rule.condition in {"above", "change_pct_above"} else value <= rule.threshold
            if triggered:
                suffix = "%" if rule.condition.startswith("change_pct") else ""
                events.append(AlertEvent(
                    rule_id=rule.id,
                    instrument_key=rule.instrument.key,
                    message=f"{quote.name}（{quote.instrument.symbol}）{labels[rule.condition]} {rule.threshold:g}{suffix}",
                    observed_value=value,
                    threshold=rule.threshold,
                    timestamp=quote.timestamp,
                ))
        return events
