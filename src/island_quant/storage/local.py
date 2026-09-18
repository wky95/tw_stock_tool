"""Content-addressed local artifact storage with a DuckDB catalog."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from island_quant.data.ports import ProviderPayload
from island_quant.storage.ports import DatasetArtifact, RawArtifact


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _canonical_records(frame: pl.DataFrame) -> bytes:
    records = frame.to_dicts()
    return json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()


class LocalArtifactStore:
    """Store immutable raw payloads and versioned normalized Parquet snapshots."""

    def __init__(
        self,
        raw_root: Path,
        normalized_root: Path,
        checkpoint_root: Path,
        catalog_path: Path,
    ) -> None:
        self.raw_root = raw_root
        self.normalized_root = normalized_root
        self.checkpoint_root = checkpoint_root
        self.catalog_path = catalog_path

    def put_raw(self, payload: ProviderPayload) -> RawArtifact:
        checksum = hashlib.sha256(payload.raw_body).hexdigest()
        directory = self.raw_root / payload.provider / payload.request.dataset
        body_path = directory / f"{checksum}.json"
        manifest_path = directory / f"{checksum}.manifest.json"
        if body_path.exists():
            if hashlib.sha256(body_path.read_bytes()).hexdigest() != checksum:
                raise RuntimeError(f"immutable raw artifact is corrupted: {body_path}")
        else:
            _atomic_bytes(body_path, payload.raw_body)
        if not manifest_path.exists():
            manifest = {
                "provider": payload.provider,
                "dataset": payload.request.dataset,
                "request": {
                    "data_id": payload.request.data_id,
                    "start_date": payload.request.start_date,
                    "end_date": payload.request.end_date,
                },
                "ingested_at": payload.fetched_at.isoformat(),
                "checksum": checksum,
                "body_path": str(body_path),
            }
            _atomic_bytes(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode(),
            )
        return RawArtifact(checksum, body_path, manifest_path)

    def read_raw(self, artifact: RawArtifact, payload: ProviderPayload) -> ProviderPayload:
        raw_body = artifact.body_path.read_bytes()
        if hashlib.sha256(raw_body).hexdigest() != artifact.checksum:
            raise RuntimeError(f"raw checksum mismatch: {artifact.body_path}")
        manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
        return ProviderPayload(
            provider=payload.provider,
            request=payload.request,
            fetched_at=datetime.fromisoformat(manifest["ingested_at"]),
            raw_body=raw_body,
        )

    def write_dataset(
        self, dataset: str, frame: pl.DataFrame, metadata: dict[str, Any]
    ) -> DatasetArtifact:
        sorted_frame = frame.sort(frame.columns) if frame.height else frame
        checksum = hashlib.sha256(_canonical_records(sorted_frame)).hexdigest()
        directory = self.normalized_root / dataset
        parquet_path = directory / f"{checksum}.parquet"
        manifest_path = directory / f"{checksum}.manifest.json"
        if not parquet_path.exists():
            parquet_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = parquet_path.with_suffix(".parquet.tmp")
            sorted_frame.write_parquet(temporary_path, compression="zstd", statistics=True)
            os.replace(temporary_path, parquet_path)
        manifest = {
            "dataset": dataset,
            "schema_version": metadata["schema_version"],
            "provider": metadata.get("provider", "derived"),
            "created_at": metadata.get("created_at", datetime.now(UTC).isoformat()),
            "checksum": checksum,
            "row_count": sorted_frame.height,
            "columns": sorted_frame.columns,
            "source_checksums": sorted(metadata.get("source_checksums", [])),
            "parquet_path": str(parquet_path),
        }
        if not manifest_path.exists():
            _atomic_bytes(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode(),
            )
        latest_path = directory / "latest.json"
        _atomic_bytes(
            latest_path,
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode(),
        )
        self._register(dataset, parquet_path)
        return DatasetArtifact(dataset, checksum, parquet_path, manifest_path, sorted_frame.height)

    def read_latest_dataset(self, dataset: str) -> pl.DataFrame | None:
        latest_path = self.normalized_root / dataset / "latest.json"
        if not latest_path.exists():
            return None
        manifest = json.loads(latest_path.read_text(encoding="utf-8"))
        path = Path(manifest["parquet_path"])
        if not path.exists():
            raise RuntimeError(f"normalized artifact is missing: {path}")
        frame = pl.read_parquet(path)
        actual = hashlib.sha256(_canonical_records(frame.sort(frame.columns))).hexdigest()
        if actual != manifest["checksum"]:
            raise RuntimeError(f"normalized checksum mismatch: {path}")
        return frame

    def load_checkpoint(self, job_id: str) -> dict[str, Any]:
        path = self.checkpoint_root / f"{job_id}.json"
        if not path.exists():
            return {"job_id": job_id, "completed": {}}
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or not isinstance(loaded.get("completed"), dict):
            raise RuntimeError(f"invalid checkpoint: {path}")
        return loaded

    def save_checkpoint(self, job_id: str, checkpoint: dict[str, Any]) -> None:
        path = self.checkpoint_root / f"{job_id}.json"
        _atomic_bytes(
            path,
            json.dumps(checkpoint, ensure_ascii=False, sort_keys=True, indent=2).encode(),
        )

    def _register(self, dataset: str, parquet_path: Path) -> None:
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        escaped_path = str(parquet_path.resolve()).replace("'", "''")
        safe_name = "".join(character if character.isalnum() else "_" for character in dataset)
        with duckdb.connect(str(self.catalog_path)) as connection:
            connection.execute(
                f'CREATE OR REPLACE VIEW "{safe_name}" AS '
                f"SELECT * FROM read_parquet('{escaped_path}')"
            )
