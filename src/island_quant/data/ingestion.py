"""Incremental, idempotent ingestion orchestration with resumable checkpoints."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import polars as pl

from island_quant.data.ports import DataRequest, HistoricalDataProvider, ProviderPayload
from island_quant.data.schema import (
    CALENDAR_DATASET,
    INSTRUMENT_DATASET,
    PRICE_DATASET,
    QUALITY_DATASET,
    UNIVERSE_DATASET,
    UNIVERSE_METADATA_DATASET,
    normalize_calendar,
    normalize_instruments,
    normalize_prices,
)
from island_quant.data.universe import UniversePolicy
from island_quant.data.validation import (
    DataQualityReport,
    DataValidationError,
    validate_daily_prices,
)
from island_quant.storage.ports import ArtifactStore, DatasetArtifact, RawArtifact


@dataclass(frozen=True, slots=True)
class IngestionResult:
    job_id: str
    artifacts: dict[str, DatasetArtifact]
    quality_report: DataQualityReport
    raw_artifact_count: int


def _job_id(
    provider: str,
    start_date: str,
    end_date: str,
    symbols: list[str] | None,
    schema_version: int,
) -> str:
    value = json.dumps(
        {
            "provider": provider,
            "start_date": start_date,
            "end_date": end_date,
            "symbols": sorted(symbols or []),
            "schema_version": schema_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(value).hexdigest()[:24]


def _merge(
    existing: pl.DataFrame | None,
    incoming: pl.DataFrame,
    keys: list[str],
    keep: Literal["first", "last"] = "first",
) -> pl.DataFrame:
    if existing is None or existing.height == 0:
        return incoming.sort(keys)
    if incoming.height == 0:
        return existing.sort(keys)
    return (
        pl.concat([existing, incoming], how="diagonal_relaxed")
        .sort([*keys, "ingested_at"])
        .unique(subset=keys, keep=keep, maintain_order=True)
        .sort(keys)
    )


class IngestionService:
    def __init__(
        self,
        provider: HistoricalDataProvider,
        store: ArtifactStore,
        policy: UniversePolicy,
        schema_version: int = 1,
    ) -> None:
        self.provider = provider
        self.store = store
        self.policy = policy
        self.schema_version = schema_version

    def run(
        self, start_date: str, end_date: str, symbols: list[str] | None = None
    ) -> IngestionResult:
        job_id = _job_id(self.provider.name, start_date, end_date, symbols, self.schema_version)
        checkpoint = self.store.load_checkpoint(job_id)
        payloads: list[tuple[ProviderPayload, RawArtifact]] = []

        def obtain(request: DataRequest) -> ProviderPayload:
            completed: dict[str, Any] = checkpoint["completed"]
            saved = completed.get(request.key)
            if saved:
                shell = ProviderPayload(
                    self.provider.name, request, datetime.now(UTC), b""
                )
                artifact = RawArtifact(
                    checksum=saved["checksum"],
                    body_path=Path(saved["body_path"]),
                    manifest_path=Path(saved["manifest_path"]),
                )
                payload = self.store.read_raw(artifact, shell)
            else:
                payload = self.provider.fetch(request)
                artifact = self.store.put_raw(payload)
                completed[request.key] = {
                    "checksum": artifact.checksum,
                    "body_path": str(artifact.body_path),
                    "manifest_path": str(artifact.manifest_path),
                }
                self.store.save_checkpoint(job_id, checkpoint)
            payloads.append((payload, artifact))
            return payload

        info = obtain(DataRequest("TaiwanStockInfo"))
        delisting = obtain(DataRequest("TaiwanStockDelisting"))
        calendar_payload = obtain(DataRequest("TaiwanStockTradingDate"))
        info_rows = info.rows()
        delisting_rows = delisting.rows()

        selected = symbols or self._discover_symbols(info_rows, delisting_rows)
        price_frames: list[pl.DataFrame] = []
        for symbol in sorted(set(selected)):
            payload = obtain(DataRequest("TaiwanStockPrice", symbol, start_date, end_date))
            price_frames.append(
                normalize_prices(
                    payload.rows(), payload.provider, payload.fetched_at, self.schema_version
                )
            )
        incoming_prices = (
            pl.concat(price_frames, how="diagonal_relaxed") if price_frames else pl.DataFrame()
        )
        prices = _merge(
            self.store.read_latest_dataset(PRICE_DATASET),
            incoming_prices,
            ["instrument_id", "trade_date"],
        )

        incoming_calendar = normalize_calendar(
            calendar_payload.rows(),
            calendar_payload.provider,
            calendar_payload.fetched_at,
            self.schema_version,
        ).filter(
            (pl.col("trade_date") >= pl.lit(start_date).str.to_date())
            & (pl.col("trade_date") <= pl.lit(end_date).str.to_date())
        )
        calendar = _merge(
            self.store.read_latest_dataset(CALENDAR_DATASET),
            incoming_calendar,
            ["trade_date"],
        )
        selected_set = set(selected)
        instruments = normalize_instruments(
            [row for row in info_rows if str(row.get("stock_id", "")) in selected_set],
            [row for row in delisting_rows if str(row.get("stock_id", "")) in selected_set],
            prices,
            info.provider,
            info.fetched_at,
            self.schema_version,
        )
        existing_instruments = self.store.read_latest_dataset(INSTRUMENT_DATASET)
        instruments = _merge(
            existing_instruments, instruments, ["instrument_id"], keep="last"
        )

        report = validate_daily_prices(
            prices,
            instruments,
            calendar,
            generated_at=max(payload.fetched_at for payload, _ in payloads),
        )
        metadata = {
            "schema_version": self.schema_version,
            "provider": self.provider.name,
            "source_checksums": [artifact.checksum for _, artifact in payloads],
            "created_at": max(payload.fetched_at for payload, _ in payloads).isoformat(),
        }
        quality_artifact = self.store.write_dataset(QUALITY_DATASET, report.to_frame(), metadata)
        if report.error_count:
            raise DataValidationError(report)

        price_artifact = self.store.write_dataset(PRICE_DATASET, prices, metadata)
        instrument_artifact = self.store.write_dataset(
            INSTRUMENT_DATASET, instruments, metadata
        )
        calendar_artifact = self.store.write_dataset(CALENDAR_DATASET, calendar, metadata)
        universe = self.policy.build_with_metadata(
            instruments,
            prices,
            calendar,
            reference_dataset_version=instrument_artifact.checksum,
        )
        artifacts = {
            PRICE_DATASET: price_artifact,
            INSTRUMENT_DATASET: instrument_artifact,
            CALENDAR_DATASET: calendar_artifact,
            UNIVERSE_DATASET: self.store.write_dataset(
                UNIVERSE_DATASET, universe.membership, metadata
            ),
            UNIVERSE_METADATA_DATASET: self.store.write_dataset(
                UNIVERSE_METADATA_DATASET, universe.metadata, metadata
            ),
            QUALITY_DATASET: quality_artifact,
        }
        checkpoint["status"] = "complete"
        checkpoint["normalized"] = {
            name: artifact.checksum for name, artifact in artifacts.items()
        }
        self.store.save_checkpoint(job_id, checkpoint)
        return IngestionResult(job_id, artifacts, report, len(payloads))

    @staticmethod
    def _discover_symbols(
        info_rows: list[dict[str, Any]], delisting_rows: list[dict[str, Any]]
    ) -> list[str]:
        symbols = {
            str(row.get("stock_id", ""))
            for row in [*info_rows, *delisting_rows]
            if re.fullmatch(r"[0-9]{4}", str(row.get("stock_id", "")))
        }
        return sorted(symbols)
