from __future__ import annotations

from datetime import date
from typing import Any

from .domain import Instrument
from .factors import FactorError, discover_factors, factor_registry, synthesize_factors
from .ports import HistoricalDataProvider


class FactorLabService:
    def __init__(self, historical_providers: dict[str, HistoricalDataProvider], research_provider: HistoricalDataProvider | None = None):
        self.historical_providers = historical_providers
        self.research_provider = research_provider

    def catalog(self) -> list[dict]:
        return factor_registry.describe()

    def discover(self, request: dict[str, Any]) -> dict:
        panel, meta = self._load_panel(request)
        result = discover_factors(
            panel,
            horizon=int(request.get("horizon", 5)),
            train_ratio=float(request.get("train_ratio", 0.7)),
            top_k=int(request.get("top_k", 6)),
            correlation_threshold=float(request.get("correlation_threshold", 0.85)),
        )
        result["meta"] = meta
        return result

    def synthesize(self, request: dict[str, Any]) -> dict:
        panel, meta = self._load_panel(request)
        factor_keys = request.get("factor_keys") or []
        if not isinstance(factor_keys, list):
            raise FactorError("factor_keys 必須是陣列")
        result = synthesize_factors(
            panel,
            [str(key) for key in factor_keys],
            method=str(request.get("method", "ic_weighted")),
            horizon=int(request.get("horizon", 5)),
            train_ratio=float(request.get("train_ratio", 0.7)),
        )
        result["meta"] = meta
        return result

    def _load_panel(self, request: dict[str, Any]):
        raw_instruments = request.get("instruments") or []
        if not isinstance(raw_instruments, list):
            raise FactorError("instruments 必須是陣列")
        instruments = list(dict.fromkeys(Instrument.parse(item) for item in raw_instruments))
        if not 3 <= len(instruments) <= 20:
            raise FactorError("橫斷面因子研究需要 3～20 檔股票，建議至少 5 檔")
        start, end = str(request.get("start", "")), str(request.get("end", ""))
        try:
            start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
        except ValueError as exc:
            raise FactorError("日期格式須為 YYYY-MM-DD") from exc
        if start_date >= end_date:
            raise FactorError("開始日期必須早於結束日期")
        if (end_date - start_date).days > 365 * 10 + 3:
            raise FactorError("因子研究區間不可超過 10 年")

        panel = {}
        for instrument in instruments:
            provider = self.research_provider or self.historical_providers.get(instrument.market)
            if not provider:
                raise FactorError(f"沒有 {instrument.market} 歷史資料 provider")
            panel[instrument.key] = provider.fetch_daily(instrument.symbol, start, end)
        common_dates = set.intersection(*(set(bar.date for bar in bars) for bars in panel.values()))
        if len(common_dates) < 30:
            raise FactorError("標的共同交易日期不足 30 日")
        return panel, {
            "instruments": [item.key for item in instruments],
            "start": start,
            "end": end,
            "common_dates": len(common_dates),
            "provider": self.research_provider.name if self.research_provider else "market_official_apis",
        }
