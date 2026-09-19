"""Sanitized, exact-version, read-only backtest artifact queries."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, cast

from island_quant.backtest.artifacts import (
    BacktestArtifactStore,
    validate_artifact_version,
)

_FORBIDDEN_KEYS = ("path", "secret", "credential", "token", "password", "api_key")


class BacktestArtifactQuery:
    def __init__(self, store: BacktestArtifactStore, version: str) -> None:
        self.version = validate_artifact_version(version)
        self._artifact = cast(dict[str, Any], _sanitize(store.read(version)))

    def summaries(self) -> list[dict[str, Any]]:
        manifest = self._manifest()
        metrics = self._metrics()
        return [
            {
                "artifact_version": self.version,
                "backtest_run_id": manifest["backtest_run_id"],
                "classification": manifest["classification"],
                "date_range": manifest["date_range"],
                "total_return": metrics["total_return"],
                "ending_equity": metrics["ending_equity"],
                "completeness_status": manifest["completeness_status"],
                "synthetic_demo": manifest["synthetic_demo"],
                "dirty": manifest["dirty"],
                "created_time": manifest["created_time"],
            }
        ]

    def detail(self, version: str) -> dict[str, Any] | None:
        validate_artifact_version(version)
        if version != self.version:
            return None
        manifest = self._manifest()
        metrics = self._metrics()
        costs = (
            _decimal_text(metrics["fees"])
            + _decimal_text(metrics["taxes"])
            + _decimal_text(metrics["slippage_cost"])
        )
        net_change = _decimal_text(metrics["ending_equity"]) - _decimal_text(
            metrics["initial_equity"]
        )
        return {
            "mode": "artifact",
            "artifact_version": self.version,
            "manifest": manifest,
            "config": self._artifact["config"],
            "performance": metrics,
            "benchmark": self._artifact["benchmark_series"],
            "warnings": self._artifact["warnings"],
            "reconciliation": self._artifact["reconciliation_report"],
            "gross_before_costs": str(net_change + costs),
            "net_after_costs": str(net_change),
            "execution_assumptions": {
                "execution_model_version": manifest["execution_model_version"],
                "fee_tax_policy_version": manifest["fee_tax_policy_version"],
                "settlement_policy_version": manifest["settlement_policy_version"],
                "risk_policy_version": manifest["risk_policy_version"],
                "mark_policy_version": manifest["mark_policy_version"],
            },
            "equity": self._equity_rows(),
            "attribution": self._attribution_payload(),
            "orders": self._order_detail(),
            "sensitivity": self._artifact["sensitivity_results"],
        }

    def equity(self, version: str) -> dict[str, Any] | None:
        validate_artifact_version(version)
        if version != self.version:
            return None
        return {
            "mode": "artifact",
            "artifact_version": self.version,
            "items": self._equity_rows(),
        }

    def _equity_rows(self) -> list[dict[str, Any]]:
        rows = []
        for snapshot in self._artifact["daily_snapshots"]:
            rows.append(
                {
                    "as_of": snapshot["as_of"],
                    "net_asset_value": snapshot["net_asset_value"],
                    "positions_market_value": snapshot["positions_market_value"],
                    "available_cash": snapshot["available_cash"],
                    "valuation_complete": snapshot["valuation_complete"],
                }
            )
        return rows

    def attribution(self, version: str) -> dict[str, Any] | None:
        validate_artifact_version(version)
        if version != self.version:
            return None
        return {
            "mode": "artifact",
            "artifact_version": self.version,
            "attribution": self._attribution_payload(),
        }

    def orders(
        self, version: str, page: int = 1, page_size: int = 20
    ) -> dict[str, Any] | None:
        validate_artifact_version(version)
        if version != self.version:
            return None
        activity = self._order_activity()
        start = (page - 1) * page_size
        return {
            "mode": "artifact",
            "artifact_version": self.version,
            "page": page,
            "page_size": page_size,
            "total": len(activity),
            "items": activity[start : start + page_size],
        }

    def _attribution_payload(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._artifact["attribution"])

    def _order_detail(self) -> dict[str, Any]:
        activity = self._order_activity()
        return {
            "orders": [item for item in activity if item["kind"] == "order"],
            "fills": [item for item in activity if item["kind"] == "fill"],
            "risk_decisions": [item for item in activity if item["kind"] == "risk"],
        }

    def _order_activity(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for event in self._artifact["orders"]:
            payload = event["payload"]
            instrument = payload["instrument"]
            rows.append(
                {
                    "kind": "order",
                    "occurred_at": event["occurred_at"],
                    "intent_id": payload["intent_id"],
                    "instrument_id": f"{instrument['market']}:{instrument['symbol']}",
                    "side": payload["side"],
                    "quantity": payload["quantity"],
                }
            )
        for event in self._artifact["fills"]:
            payload = event["payload"]
            fill = payload.get("fill")
            rows.append(
                {
                    "kind": "fill",
                    "occurred_at": event["occurred_at"],
                    "intent_id": payload["intent_id"],
                    "status": payload["status"],
                    "instrument_id": (
                        f"{fill['instrument']['market']}:{fill['instrument']['symbol']}"
                        if fill is not None
                        else None
                    ),
                    "side": fill["side"] if fill is not None else None,
                    "quantity": fill["quantity"] if fill is not None else 0,
                    "execution_price": fill["price"] if fill is not None else None,
                    "reference_price": payload.get("reference_price"),
                }
            )
        for event in self._artifact["risk_decisions"]:
            payload = event["payload"]
            rows.append(
                {
                    "kind": "risk",
                    "occurred_at": event["occurred_at"],
                    "intent_id": payload["intent_id"],
                    "outcome": payload["outcome"],
                    "reasons": payload["reasons"],
                    "rule_version": payload["rule_version"],
                }
            )
        return sorted(rows, key=lambda item: (str(item["occurred_at"]), str(item["kind"])))

    def _manifest(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._artifact["manifest"])

    def _metrics(self) -> dict[str, Any]:
        return cast(dict[str, Any], self._artifact["performance_metrics"])


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if any(forbidden in normalized for forbidden in _FORBIDDEN_KEYS):
                continue
            result[str(key)] = _sanitize(item)
        return result
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str) and (
        value.startswith(("/", "file://", "\\\\"))
        or re.match(r"^[A-Za-z]:[\\/]", value) is not None
    ):
        return "[redacted-path]"
    return value


def _decimal_text(value: object) -> Decimal:
    return Decimal(str(value))
