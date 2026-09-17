"""Versioned HTTP API controller, independent from the HTTP server implementation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .data import DataError, TpexHistoricalProvider, TwseHistoricalProvider, parse_csv_text
from .engine import BacktestConfig, BacktestError, run_backtest
from .factor_service import FactorLabService
from .monitoring import MonitoringService
from .ports import Broker, HistoricalDataProvider, QuoteProvider
from .providers.mis import QuoteProviderError
from .strategies import strategy_registry


@dataclass(frozen=True)
class ApiResponse:
    status: int
    payload: dict[str, Any]


class ApiController:
    def __init__(
        self,
        historical_providers: dict[str, HistoricalDataProvider],
        quote_provider: QuoteProvider,
        broker: Broker,
        research_provider: HistoricalDataProvider | None = None,
    ):
        self.historical_providers = historical_providers
        self.quote_provider = quote_provider
        self.broker = broker
        self.monitoring = MonitoringService(quote_provider)
        self.factor_lab = FactorLabService(historical_providers, research_provider)

    def handle(self, method: str, path: str, query: dict[str, list[str]], body: Any = None) -> ApiResponse:
        try:
            if method == "GET" and path in {"/api/health", "/api/v1/health"}:
                return self._ok({"status": "healthy", "api_version": "v1"})
            if method == "GET" and path == "/api/v1/capabilities":
                return self._ok({
                    "api_version": "v1",
                    "historical_providers": {key: value.name for key, value in self.historical_providers.items()},
                    "quote_provider": self.quote_provider.name,
                    "broker": self.broker.name,
                    "live_order_execution": self.broker.name != "disabled",
                    "strategies": strategy_registry.describe(),
                    "factor_count": len(self.factor_lab.catalog()),
                    "research_provider": self.factor_lab.research_provider.name if self.factor_lab.research_provider else "market_official_apis",
                })
            if method == "GET" and path == "/api/v1/openapi.json":
                return ApiResponse(200, openapi_spec())
            if method == "POST" and path in {"/api/backtest", "/api/v1/backtests"}:
                return self._backtest(self._object(body))
            if method == "POST" and path == "/api/v1/monitor/snapshot":
                request = self._object(body)
                result = self.monitoring.snapshot(request.get("instruments") or [], request.get("alerts") or [])
                return self._ok(result)
            if method == "GET" and path == "/api/v1/factors/catalog":
                return self._ok(self.factor_lab.catalog())
            if method == "POST" and path == "/api/v1/factors/discover":
                return self._ok(self.factor_lab.discover(self._object(body)))
            if method == "POST" and path == "/api/v1/factors/synthesize":
                return self._ok(self.factor_lab.synthesize(self._object(body)))
            return ApiResponse(404, {"ok": False, "error": {"code": "not_found", "message": "找不到 API 路徑"}})
        except (DataError, BacktestError, ValueError, TypeError) as exc:
            return ApiResponse(400, {"ok": False, "error": {"code": "invalid_request", "message": str(exc)}})
        except QuoteProviderError as exc:
            return ApiResponse(502, {"ok": False, "error": {"code": "upstream_unavailable", "message": str(exc)}})

    def _backtest(self, body: dict[str, Any]) -> ApiResponse:
        if body.get("source") == "csv":
            bars = parse_csv_text(str(body.get("csv_text", "")))
            source_name, symbol = "CSV 匯入", "CSV"
        else:
            symbol = str(body.get("symbol", ""))
            market = str(body.get("market", "tse")).lower()
            if market not in self.historical_providers:
                raise ValueError("歷史資料市場僅支援 tse / otc")
            provider = self.historical_providers[market]
            bars = provider.fetch_daily(symbol, str(body.get("start", "")), str(body.get("end", "")))
            source_name = provider.name
        config = BacktestConfig.from_dict(body.get("config") or {})
        result = run_backtest(bars, str(body.get("strategy", "sma_cross")), body.get("params") or {}, config)
        result["meta"] = {
            "symbol": symbol,
            "start": bars[0].date,
            "end": bars[-1].date,
            "bar_count": len(bars),
            "source": source_name,
        }
        return self._ok(result)

    @staticmethod
    def _object(body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ValueError("JSON request body 必須是物件")
        return body

    @staticmethod
    def _ok(result: Any) -> ApiResponse:
        return ApiResponse(200, {"ok": True, "result": result})


def default_controller(quote_provider: QuoteProvider, broker: Broker, research_provider: HistoricalDataProvider | None = None) -> ApiController:
    return ApiController({"tse": TwseHistoricalProvider(), "otc": TpexHistoricalProvider()}, quote_provider, broker, research_provider)


def openapi_spec() -> dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {"title": "TW Stock Toolkit API", "version": "1.0.0"},
        "paths": {
            "/api/v1/health": {"get": {"summary": "健康檢查"}},
            "/api/v1/capabilities": {"get": {"summary": "已啟用的 providers 與策略"}},
            "/api/v1/backtests": {"post": {"summary": "執行日 K 回測"}},
            "/api/v1/monitor/snapshot": {"post": {"summary": "取得觀察清單快照並評估警示"}},
            "/api/v1/factors/catalog": {"get": {"summary": "列出可用日 K 因子"}},
            "/api/v1/factors/discover": {"post": {"summary": "執行橫斷面因子挖掘"}},
            "/api/v1/factors/synthesize": {"post": {"summary": "合成因子並進行樣本外評估"}},
        },
    }
