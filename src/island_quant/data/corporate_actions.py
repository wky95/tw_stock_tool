"""Provider-neutral corporate-action records and revision-safe normalization."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

import polars as pl


class CorporateActionType(StrEnum):
    CASH_DIVIDEND = "cash_dividend"
    STOCK_DIVIDEND = "stock_dividend"
    SPLIT = "split"
    REVERSE_SPLIT = "reverse_split"
    CAPITAL_REDUCTION = "capital_reduction"
    RIGHTS_ISSUE = "rights_issue"
    SYMBOL_CHANGE = "symbol_change"
    LISTING = "listing"
    DELISTING = "delisting"


class ActionQuality(StrEnum):
    CONFIRMED = "confirmed"
    PROVISIONAL = "provisional"


class CapitalReductionSubtype(StrEnum):
    LOSS_OFFSET = "loss_offset"
    CASH = "cash"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CorporateAction:
    instrument_id: str
    action_type: CorporateActionType
    announcement_time: datetime | None
    ex_date: date | None
    effective_date: date
    record_date: date | None
    payment_date: date | None
    ratio: float | None
    cash_amount: float | None
    currency: str | None
    source: str
    source_event_id: str
    ingested_at: datetime
    available_at: datetime | None
    revision: int = 1
    quality: ActionQuality = ActionQuality.CONFIRMED
    old_symbol: str | None = None
    new_symbol: str | None = None
    rights_subscription_price: float | None = None
    capital_reduction_subtype: CapitalReductionSubtype | None = None

    def __post_init__(self) -> None:
        for name in ("announcement_time", "ingested_at", "available_at"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")
        if self.revision < 1:
            raise ValueError("revision must be positive")
        if self.ratio is not None and self.ratio <= 0:
            raise ValueError(
                "ratio must be positive and mean post-action shares / pre-action shares"
            )
        if self.cash_amount is not None and self.cash_amount < 0:
            raise ValueError("cash_amount cannot be negative")
        if self.rights_subscription_price is not None and self.rights_subscription_price < 0:
            raise ValueError("rights_subscription_price cannot be negative")
        if self.available_at is None and self.quality is not ActionQuality.PROVISIONAL:
            raise ValueError("unknown available_at must be marked provisional")
        if (
            self.available_at is not None
            and self.announcement_time is not None
            and self.available_at < self.announcement_time
        ):
            raise ValueError("available_at cannot precede announcement_time")
        if self.action_type is CorporateActionType.CASH_DIVIDEND and self.cash_amount is None:
            raise ValueError("cash dividend requires cash_amount")
        if (
            self.action_type
            in {
                CorporateActionType.STOCK_DIVIDEND,
                CorporateActionType.SPLIT,
                CorporateActionType.REVERSE_SPLIT,
                CorporateActionType.CAPITAL_REDUCTION,
            }
            and self.ratio is None
        ):
            raise ValueError(f"{self.action_type.value} requires ratio")
        if self.action_type is CorporateActionType.RIGHTS_ISSUE and self.ratio is None:
            raise ValueError("rights issue requires ratio")
        if self.action_type is CorporateActionType.SYMBOL_CHANGE and not self.new_symbol:
            raise ValueError("symbol change requires new_symbol")
        if (
            self.action_type is CorporateActionType.CAPITAL_REDUCTION
            and self.capital_reduction_subtype is None
        ):
            raise ValueError("capital reduction requires an explicit subtype")
        if (
            self.capital_reduction_subtype is CapitalReductionSubtype.UNKNOWN
            and self.quality is not ActionQuality.PROVISIONAL
        ):
            raise ValueError("unknown capital reduction subtype must be provisional")

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["action_type"] = self.action_type.value
        record["quality"] = self.quality.value
        if self.capital_reduction_subtype is not None:
            record["capital_reduction_subtype"] = self.capital_reduction_subtype.value
        return record


def corporate_actions_frame(actions: list[CorporateAction]) -> pl.DataFrame:
    """Preserve every revision; reject only duplicate revision identities."""
    identities: set[tuple[str, str, int]] = set()
    records: list[dict[str, Any]] = []
    for action in actions:
        identity = (action.source, action.source_event_id, action.revision)
        if identity in identities:
            raise ValueError(f"duplicate corporate-action revision: {identity}")
        identities.add(identity)
        records.append(action.to_record())
    return pl.DataFrame(records, infer_schema_length=None)


def effective_actions(actions: pl.DataFrame, decision_time: datetime | None = None) -> pl.DataFrame:
    """Select the latest usable revision, optionally as known at a decision time."""
    selected = actions
    if decision_time is not None:
        if decision_time.tzinfo is None or decision_time.utcoffset() is None:
            raise ValueError("decision_time must be timezone-aware")
        selected = selected.filter(
            pl.col("available_at").is_not_null() & (pl.col("available_at") <= decision_time)
        )
    if not selected.height:
        return selected
    return (
        selected.sort(["source", "source_event_id", "revision"])
        .group_by(["source", "source_event_id"], maintain_order=True)
        .tail(1)
        .sort(
            [
                "instrument_id",
                "effective_date",
                "action_type",
                "source",
                "source_event_id",
                "revision",
            ]
        )
    )
