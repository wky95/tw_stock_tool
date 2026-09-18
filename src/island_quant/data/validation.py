"""Deterministic data-quality checks with machine-readable findings."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum

import polars as pl


class Severity(StrEnum):
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class QualityIssue:
    severity: Severity
    code: str
    dataset: str
    primary_key: str
    message: str


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    generated_at: datetime
    issues: tuple[QualityIssue, ...]

    @property
    def error_count(self) -> int:
        return sum(issue.severity is Severity.ERROR for issue in self.issues)

    @property
    def warning_count(self) -> int:
        return sum(issue.severity is Severity.WARNING for issue in self.issues)

    def to_frame(self) -> pl.DataFrame:
        rows = []
        for issue in self.issues:
            row = asdict(issue)
            row["severity"] = issue.severity.value
            row["generated_at"] = self.generated_at
            rows.append(row)
        if not rows:
            return pl.DataFrame(
                schema={
                    "severity": pl.String,
                    "code": pl.String,
                    "dataset": pl.String,
                    "primary_key": pl.String,
                    "message": pl.String,
                    "generated_at": pl.Datetime(time_zone="UTC"),
                }
            )
        return pl.DataFrame(rows, infer_schema_length=None)


class DataValidationError(ValueError):
    def __init__(self, report: DataQualityReport) -> None:
        self.report = report
        super().__init__(f"data validation failed with {report.error_count} error(s)")


def validate_daily_prices(
    prices: pl.DataFrame,
    instruments: pl.DataFrame,
    calendar: pl.DataFrame,
    generated_at: datetime | None = None,
) -> DataQualityReport:
    issues: list[QualityIssue] = []
    duplicates = prices.group_by(["instrument_id", "trade_date"]).len().filter(pl.col("len") > 1)
    for row in duplicates.to_dicts():
        key = f"{row['instrument_id']}:{row['trade_date']}"
        issues.append(
            QualityIssue(Severity.ERROR, "DUPLICATE_PRIMARY_KEY", "daily_prices", key, key)
        )

    for row in prices.to_dicts():
        key = f"{row['instrument_id']}:{row['trade_date']}"
        values = [row["open"], row["high"], row["low"], row["close"]]
        if any(value < 0 for value in values):
            issues.append(QualityIssue(Severity.ERROR, "NEGATIVE_PRICE", "daily_prices", key, key))
            continue
        if row["volume"] < 0 or row["traded_value"] < 0 or row["turnover"] < 0:
            issues.append(
                QualityIssue(Severity.ERROR, "NEGATIVE_ACTIVITY", "daily_prices", key, key)
            )
        if all(value == 0 for value in values):
            issues.append(
                QualityIssue(
                    Severity.WARNING,
                    "NO_PUBLISHED_PRICE",
                    "daily_prices",
                    key,
                    "provider reported zero for every OHLC field",
                )
            )
        elif any(value <= 0 for value in values):
            issues.append(
                QualityIssue(Severity.ERROR, "PARTIAL_ZERO_PRICE", "daily_prices", key, key)
            )
        elif row["high"] < max(row["open"], row["close"]) or row["low"] > min(
            row["open"], row["close"]
        ):
            issues.append(
                QualityIssue(Severity.ERROR, "INVALID_OHLC", "daily_prices", key, key)
            )

    expected_dates = set(calendar.get_column("trade_date").to_list())
    actual_by_symbol: dict[str, set[object]] = {}
    for row in prices.select("instrument_id", "trade_date").to_dicts():
        actual_by_symbol.setdefault(row["instrument_id"], set()).add(row["trade_date"])
    for instrument in instruments.to_dicts():
        if instrument["security_type"] != "ordinary_share":
            continue
        if instrument["market"] == "unknown":
            issues.append(
                QualityIssue(
                    Severity.WARNING,
                    "UNKNOWN_HISTORICAL_MARKET",
                    "instrument_master",
                    instrument["instrument_id"],
                    "provider history cannot establish TWSE or TPEx; excluded fail-closed",
                )
            )
            continue
        listing_date = instrument["listing_date"]
        delisting_date = instrument["delisting_date"]
        if listing_date is None:
            issues.append(
                QualityIssue(
                    Severity.ERROR,
                    "MISSING_LISTING_DATE",
                    "instrument_master",
                    instrument["instrument_id"],
                    "listing date could not be derived from price history",
                )
            )
            continue
        active_dates = {
            day
            for day in expected_dates
            if day >= listing_date and (delisting_date is None or day < delisting_date)
        }
        missing = sorted(active_dates - actual_by_symbol.get(instrument["instrument_id"], set()))
        for day in missing:
            key = f"{instrument['instrument_id']}:{day}"
            issues.append(
                QualityIssue(
                    Severity.WARNING,
                    "MISSING_TRADING_DAY",
                    "daily_prices",
                    key,
                    "active instrument has no daily row on a calendar trading day",
                )
            )
    return DataQualityReport(generated_at or datetime.now(UTC), tuple(issues))
