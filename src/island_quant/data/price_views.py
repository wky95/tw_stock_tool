"""Canonical unadjusted, split-adjusted, and total-return price views."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from typing import Any

import polars as pl

from island_quant.data.availability import AvailabilityPolicy
from island_quant.data.corporate_actions import (
    CapitalReductionSubtype,
    CorporateActionType,
    effective_actions,
)

ADJUSTMENT_ALGORITHM_VERSION = "tw-backward-adjustment-v1"


class UnsupportedCorporateActionError(ValueError):
    def __init__(self, code: str, event_ids: list[str]) -> None:
        self.code = code
        self.event_ids = sorted(event_ids)
        super().__init__(f"{code}: {','.join(self.event_ids)}")


@dataclass(frozen=True, slots=True)
class PriceViewBundle:
    canonical_unadjusted: pl.DataFrame
    split_adjusted: pl.DataFrame
    total_return: pl.DataFrame
    adjustment_factors: pl.DataFrame


def _action_rows(actions: pl.DataFrame, decision_time: datetime | None) -> list[dict[str, Any]]:
    if not actions.height:
        return []
    return effective_actions(actions, decision_time).to_dicts()


def _cash_factor(action: dict[str, Any], ordered_prices: list[dict[str, Any]]) -> float:
    ex_date = action.get("ex_date") or action["effective_date"]
    previous = [row for row in ordered_prices if row["trade_date"] < ex_date]
    if not previous:
        raise ValueError(f"cash action has no previous close: {action['source_event_id']}")
    previous_close = float(previous[-1]["close"])
    amount = float(action["cash_amount"])
    if previous_close <= 0 or amount >= previous_close:
        raise ValueError(f"invalid cash adjustment basis: {action['source_event_id']}")
    return (previous_close - amount) / previous_close


def build_price_views(
    raw_unadjusted: pl.DataFrame,
    corporate_actions: pl.DataFrame,
    *,
    raw_dataset_version: str,
    corporate_action_version: str,
    availability_policy: AvailabilityPolicy,
    decision_time: datetime | None = None,
) -> PriceViewBundle:
    """Build immutable price views; analytical views explicitly permit future actions."""
    required = {"instrument_id", "trade_date", "open", "high", "low", "close", "volume"}
    if not required.issubset(raw_unadjusted.columns):
        raise ValueError(f"raw prices require columns: {sorted(required)}")
    semantics = "analytical_backward_adjusted" if decision_time is None else "point_in_time"
    uses_future_actions = decision_time is None
    action_rows = _action_rows(corporate_actions, decision_time)
    _validate_supported_actions(action_rows)
    canonical_rows: list[dict[str, Any]] = []
    split_rows: list[dict[str, Any]] = []
    total_rows: list[dict[str, Any]] = []
    factor_rows: list[dict[str, Any]] = []
    for instrument_id in raw_unadjusted["instrument_id"].unique().sort().to_list():
        prices = (
            raw_unadjusted.filter(pl.col("instrument_id") == instrument_id)
            .sort("trade_date")
            .to_dicts()
        )
        instrument_actions = [row for row in action_rows if row["instrument_id"] == instrument_id]
        split_actions = [
            row
            for row in instrument_actions
            if row["action_type"]
            in {
                CorporateActionType.STOCK_DIVIDEND.value,
                CorporateActionType.SPLIT.value,
                CorporateActionType.REVERSE_SPLIT.value,
            }
            or (
                row["action_type"] == CorporateActionType.CAPITAL_REDUCTION.value
                and row.get("capital_reduction_subtype")
                == CapitalReductionSubtype.LOSS_OFFSET.value
            )
        ]
        cash_actions = [
            (row, _cash_factor(row, prices))
            for row in instrument_actions
            if row["action_type"] == CorporateActionType.CASH_DIVIDEND.value
        ]
        for raw in prices:
            day: date = raw["trade_date"]
            canonical = dict(raw)
            canonical["event_time"] = availability_policy.event_time(day)
            canonical["available_at"] = availability_policy.available_at(day)
            canonical["availability_policy_version"] = availability_policy.version
            canonical["raw_dataset_version"] = raw_dataset_version
            canonical["price_view"] = "canonical_unadjusted"
            canonical_rows.append(canonical)

            future_splits = [row for row in split_actions if row["effective_date"] > day]
            share_factor = 1.0
            for action in future_splits:
                share_factor *= float(action["ratio"])
            split_price_factor = 1.0 / share_factor
            cash_factor = 1.0
            for action, action_factor in cash_actions:
                action_date = action.get("ex_date") or action["effective_date"]
                if action_date > day:
                    cash_factor *= action_factor
            total_factor = split_price_factor * cash_factor
            if not all(
                isfinite(value) and value > 0
                for value in (split_price_factor, share_factor, cash_factor, total_factor)
            ):
                raise ValueError(
                    f"non-positive or non-finite adjustment factor: {instrument_id}@{day}"
                )
            lineage = {
                "raw_dataset_version": raw_dataset_version,
                "corporate_action_version": corporate_action_version,
                "adjustment_algorithm_version": ADJUSTMENT_ALGORITHM_VERSION,
                "view_semantics": semantics,
                "uses_future_actions": uses_future_actions,
            }
            factor_rows.append(
                {
                    "instrument_id": instrument_id,
                    "trade_date": day,
                    "split_price_factor": split_price_factor,
                    "split_volume_factor": share_factor,
                    "cash_total_return_factor": cash_factor,
                    "total_return_price_factor": total_factor,
                    **lineage,
                }
            )
            split_row = _adjusted_row(canonical, split_price_factor, share_factor)
            split_row.update(lineage)
            split_row["price_view"] = "split_adjusted"
            split_rows.append(split_row)
            total_row = _adjusted_row(canonical, total_factor, share_factor)
            total_row.update(lineage)
            total_row["price_view"] = "total_return"
            total_rows.append(total_row)
    _validate_adjusted_ohlc(split_rows)
    _validate_adjusted_ohlc(total_rows)
    return PriceViewBundle(
        canonical_unadjusted=pl.DataFrame(canonical_rows, infer_schema_length=None),
        split_adjusted=pl.DataFrame(split_rows, infer_schema_length=None),
        total_return=pl.DataFrame(total_rows, infer_schema_length=None),
        adjustment_factors=pl.DataFrame(factor_rows, infer_schema_length=None),
    )


def _adjusted_row(
    canonical: dict[str, Any], price_factor: float, volume_factor: float
) -> dict[str, Any]:
    adjusted = dict(canonical)
    for column in ("open", "high", "low", "close"):
        adjusted[column] = float(canonical[column]) * price_factor
    adjusted["volume"] = round(float(canonical["volume"]) * volume_factor)
    adjusted["volume_unit"] = "shares"
    return adjusted


def _validate_adjusted_ohlc(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        open_price = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        if (
            min(open_price, high, low, close) < 0
            or high < max(open_price, close, low)
            or low > min(open_price, close, high)
        ):
            raise ValueError(
                f"adjustment produced invalid OHLC: {row['instrument_id']}@{row['trade_date']}"
            )


def _validate_supported_actions(actions: list[dict[str, Any]]) -> None:
    unsupported = [
        row
        for row in actions
        if row["action_type"] == CorporateActionType.RIGHTS_ISSUE.value
        or (
            row["action_type"] == CorporateActionType.CAPITAL_REDUCTION.value
            and row.get("capital_reduction_subtype")
            != CapitalReductionSubtype.LOSS_OFFSET.value
        )
    ]
    if unsupported:
        raise UnsupportedCorporateActionError(
            "UNSUPPORTED_CORPORATE_ACTION_ADJUSTMENT",
            [str(row["source_event_id"]) for row in unsupported],
        )
    adjusting_types = {
        CorporateActionType.CASH_DIVIDEND.value,
        CorporateActionType.STOCK_DIVIDEND.value,
        CorporateActionType.SPLIT.value,
        CorporateActionType.REVERSE_SPLIT.value,
        CorporateActionType.CAPITAL_REDUCTION.value,
    }
    grouped: dict[tuple[str, date], list[dict[str, Any]]] = {}
    for row in actions:
        if row["action_type"] in adjusting_types:
            grouped.setdefault((str(row["instrument_id"]), row["effective_date"]), []).append(row)
    ambiguous = [rows for rows in grouped.values() if len(rows) > 1]
    if ambiguous:
        raise UnsupportedCorporateActionError(
            "UNSUPPORTED_SAME_DAY_CORPORATE_ACTIONS",
            [str(row["source_event_id"]) for rows in ambiguous for row in rows],
        )
