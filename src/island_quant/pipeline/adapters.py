"""Provider-neutral typed readers over exact-version pipeline artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from island_quant.backtest.artifacts import BacktestArtifact, BacktestArtifactStore
from island_quant.pipeline.artifacts import ExactArtifactStore


@dataclass(frozen=True, slots=True)
class MarketDataRecord:
    instrument_id: str
    market: str
    trade_date: date
    event_time: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True, slots=True)
class UniverseSnapshotRecord:
    instrument_id: str
    trade_date: date
    eligible: bool
    exclusion_reasons: tuple[str, ...]
    listing_date: date
    delisting_date: date | None


@dataclass(frozen=True, slots=True)
class CalendarRecord:
    trade_date: date
    market: str
    is_open: bool
    status: str


@dataclass(frozen=True, slots=True)
class CorporateActionRecord:
    action_id: str
    instrument_id: str
    action_type: str
    announcement_time: datetime
    effective_date: date
    record_date: date | None
    payment_date: date | None
    value: Decimal


@dataclass(frozen=True, slots=True)
class FeatureArtifactRecord:
    instrument_id: str
    decision_time: datetime
    feature_name: str
    value: Decimal | None
    valid: bool


@dataclass(frozen=True, slots=True)
class LabelArtifactRecord:
    instrument_id: str
    decision_time: datetime
    label_version: str
    value: Decimal | None
    valid: bool


@dataclass(frozen=True, slots=True)
class BenchmarkRecord:
    benchmark_id: str
    trade_date: date
    level: Decimal
    available_at: datetime


T = TypeVar("T")


class _Reader[T]:
    artifact_type: str
    schema_version = 1

    def __init__(self, root: Path) -> None:
        self.store = ExactArtifactStore(root, self.artifact_type, self.schema_version)

    def read_batches(self, version: str, *, batch_size: int = 1000):  # type: ignore[no-untyped-def]
        for batch in self.store.batches(version, batch_size=batch_size):
            yield tuple(self._decode(item) for item in batch.records)

    def _decode(self, item: dict[str, Any]) -> T:
        raise NotImplementedError


class NormalizedMarketDataReader(_Reader[MarketDataRecord]):
    artifact_type = "normalized_market_data"

    def _decode(self, item: dict[str, Any]) -> MarketDataRecord:
        record = MarketDataRecord(
            str(item["instrument_id"]),
            str(item["market"]),
            date.fromisoformat(item["trade_date"]),
            datetime.fromisoformat(item["event_time"]),
            datetime.fromisoformat(item["available_at"]),
            Decimal(str(item["open"])),
            Decimal(str(item["high"])),
            Decimal(str(item["low"])),
            Decimal(str(item["close"])),
            int(item["volume"]),
        )
        if record.event_time.tzinfo is None or record.available_at.tzinfo is None:
            raise ValueError("market data timestamps must be timezone-aware")
        if min(record.open, record.high, record.low, record.close) <= 0 or record.volume < 0:
            raise ValueError("market data values are invalid")
        return record


class UniverseSnapshotReader(_Reader[UniverseSnapshotRecord]):
    artifact_type = "universe_snapshots"

    def _decode(self, item: dict[str, Any]) -> UniverseSnapshotRecord:
        return UniverseSnapshotRecord(
            str(item["instrument_id"]),
            date.fromisoformat(item["trade_date"]),
            bool(item["eligible"]),
            tuple(str(value) for value in item["exclusion_reasons"]),
            date.fromisoformat(item["listing_date"]),
            date.fromisoformat(item["delisting_date"]) if item.get("delisting_date") else None,
        )


class CalendarReader(_Reader[CalendarRecord]):
    artifact_type = "trading_calendars"

    def _decode(self, item: dict[str, Any]) -> CalendarRecord:
        return CalendarRecord(
            date.fromisoformat(item["trade_date"]),
            str(item["market"]),
            bool(item["is_open"]),
            str(item["status"]),
        )


class CorporateActionReader(_Reader[CorporateActionRecord]):
    artifact_type = "corporate_actions"

    def _decode(self, item: dict[str, Any]) -> CorporateActionRecord:
        record = CorporateActionRecord(
            str(item["action_id"]),
            str(item["instrument_id"]),
            str(item["action_type"]),
            datetime.fromisoformat(item["announcement_time"]),
            date.fromisoformat(item["effective_date"]),
            date.fromisoformat(item["record_date"]) if item.get("record_date") else None,
            date.fromisoformat(item["payment_date"]) if item.get("payment_date") else None,
            Decimal(str(item["value"])),
        )
        if record.announcement_time.tzinfo is None:
            raise ValueError("corporate action announcement must be timezone-aware")
        return record


class FeatureArtifactReader(_Reader[FeatureArtifactRecord]):
    artifact_type = "features"

    def _decode(self, item: dict[str, Any]) -> FeatureArtifactRecord:
        return FeatureArtifactRecord(
            str(item["instrument_id"]),
            datetime.fromisoformat(item["decision_time"]),
            str(item["feature_name"]),
            Decimal(str(item["value"])) if item.get("value") is not None else None,
            bool(item["valid"]),
        )


class LabelArtifactReader(_Reader[LabelArtifactRecord]):
    artifact_type = "labels"

    def _decode(self, item: dict[str, Any]) -> LabelArtifactRecord:
        return LabelArtifactRecord(
            str(item["instrument_id"]),
            datetime.fromisoformat(item["decision_time"]),
            str(item["label_version"]),
            Decimal(str(item["value"])) if item.get("value") is not None else None,
            bool(item["valid"]),
        )


class BenchmarkReader(_Reader[BenchmarkRecord]):
    artifact_type = "benchmarks"

    def _decode(self, item: dict[str, Any]) -> BenchmarkRecord:
        return BenchmarkRecord(
            str(item["benchmark_id"]),
            date.fromisoformat(item["trade_date"]),
            Decimal(str(item["level"])),
            datetime.fromisoformat(item["available_at"]),
        )


class BacktestArtifactReader:
    """Typed boundary around the existing exact-version backtest store."""

    def __init__(self, root: Path) -> None:
        self.store = BacktestArtifactStore(root)

    def read(self, version: str) -> dict[str, Any]:
        return self.store.read(version)


class BacktestArtifactWriter:
    """Immutable writer boundary; construction remains in the application layer."""

    def __init__(self, root: Path) -> None:
        self.store = BacktestArtifactStore(root)

    def write(self, artifact: BacktestArtifact) -> Path:
        return self.store.save(artifact)
