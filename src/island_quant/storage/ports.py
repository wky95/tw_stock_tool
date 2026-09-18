"""Storage protocols keep local files replaceable by future object storage."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import polars as pl

from island_quant.data.ports import ProviderPayload


@dataclass(frozen=True, slots=True)
class RawArtifact:
    checksum: str
    body_path: Path
    manifest_path: Path


@dataclass(frozen=True, slots=True)
class DatasetArtifact:
    dataset: str
    checksum: str
    parquet_path: Path
    manifest_path: Path
    row_count: int


class ArtifactStore(Protocol):
    def put_raw(self, payload: ProviderPayload) -> RawArtifact: ...

    def read_raw(self, artifact: RawArtifact, payload: ProviderPayload) -> ProviderPayload: ...

    def write_dataset(
        self, dataset: str, frame: pl.DataFrame, metadata: dict[str, Any]
    ) -> DatasetArtifact: ...

    def stage_dataset(
        self, dataset: str, frame: pl.DataFrame, metadata: dict[str, Any]
    ) -> DatasetArtifact: ...

    def promote_dataset(
        self,
        dataset: str,
        version: str,
        *,
        expected_current_version: str | None = None,
        validated: bool = False,
    ) -> DatasetArtifact: ...

    def read_dataset_version(self, dataset: str, version: str) -> pl.DataFrame: ...

    def current_version(self, dataset: str) -> str | None: ...

    def read_latest_dataset(self, dataset: str) -> pl.DataFrame | None: ...

    def load_checkpoint(self, job_id: str) -> dict[str, Any]: ...

    def save_checkpoint(self, job_id: str, checkpoint: dict[str, Any]) -> None: ...
