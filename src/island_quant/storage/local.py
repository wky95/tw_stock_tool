"""Content-addressed local artifact storage with a DuckDB catalog."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from island_quant.data.ports import ProviderPayload
from island_quant.storage.ports import DatasetArtifact, RawArtifact
from island_quant.storage.provenance import CodeProvenance, capture_code_provenance


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
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            # Directory fsync is unavailable on some filesystems; file fsync + replace remains.
            pass
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
        provenance_provider: Callable[[], CodeProvenance] = capture_code_provenance,
    ) -> None:
        self.raw_root = raw_root
        self.normalized_root = normalized_root
        self.checkpoint_root = checkpoint_root
        self.catalog_path = catalog_path
        self.provenance_provider = provenance_provider

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
        artifact = self.stage_dataset(dataset, frame, metadata)
        return self.promote_dataset(dataset, artifact.checksum)

    def stage_dataset(
        self, dataset: str, frame: pl.DataFrame, metadata: dict[str, Any]
    ) -> DatasetArtifact:
        """Write an immutable candidate without changing the current pointer."""
        sorted_frame = frame.sort(frame.columns) if frame.height else frame
        data_checksum = hashlib.sha256(_canonical_records(sorted_frame)).hexdigest()
        provenance = self.provenance_provider()
        canonical_configuration = json.dumps(
            {
                "configuration_version": metadata.get(
                    "configuration_version", "unspecified"
                ),
                "values": metadata.get("configuration", {}),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        configuration_hash = hashlib.sha256(canonical_configuration).hexdigest()
        identity = {
            "data_checksum": data_checksum,
            "schema_version": metadata["schema_version"],
            "source_checksums": sorted(metadata.get("source_checksums", [])),
            "transformation_algorithm_version": metadata.get(
                "transformation_algorithm_version",
                metadata.get("transformation_code_version", "unspecified"),
            ),
            "code_version": metadata.get("code_version", "0.1.0"),
            "configuration_hash": configuration_hash,
            "git_commit": provenance.git_commit,
            "dirty": provenance.dirty,
            "source_tree_hash": provenance.source_tree_hash,
        }
        checksum = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
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
            "data_checksum": data_checksum,
            "row_count": sorted_frame.height,
            "columns": sorted_frame.columns,
            "source_checksums": sorted(metadata.get("source_checksums", [])),
            "git_commit": provenance.git_commit,
            "dirty": provenance.dirty,
            "source_tree_hash": provenance.source_tree_hash,
            "code_version": identity["code_version"],
            "configuration_hash": configuration_hash,
            "transformation_code_version": metadata.get(
                "transformation_code_version", "unspecified"
            ),
            "configuration_version": metadata.get("configuration_version", "unspecified"),
            "transformation_algorithm_version": identity[
                "transformation_algorithm_version"
            ],
            "quality_flags": sorted(metadata.get("quality_flags", [])),
            "status": "candidate",
            "parquet_path": str(parquet_path),
        }
        if not manifest_path.exists():
            _atomic_bytes(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode(),
            )
        return DatasetArtifact(dataset, checksum, parquet_path, manifest_path, sorted_frame.height)

    def promote_dataset(
        self,
        dataset: str,
        version: str,
        *,
        expected_current_version: str | None = None,
        validated: bool = False,
    ) -> DatasetArtifact:
        """Atomically promote a verified candidate to the current dataset version."""
        directory = self.normalized_root / dataset
        manifest_path = directory / f"{version}.manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"candidate manifest not found: {manifest_path}")
        with self._promotion_lock(directory):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self._verify_candidate_identity(dataset, version, manifest, manifest_path)
            if validated and bool(manifest["dirty"]):
                raise RuntimeError("validated promotion rejects dirty-worktree artifacts")
            current_path = directory / "current.json"
            current = self.current_version(dataset)
            if expected_current_version is not None and current != expected_current_version:
                raise RuntimeError(
                    f"promotion compare-and-swap failed: expected {expected_current_version}, "
                    f"found {current}"
                )
            parquet_path = Path(manifest["parquet_path"])
            frame = self._read_verified(
                parquet_path, str(manifest.get("data_checksum", manifest["checksum"]))
            )
            self._register(dataset, parquet_path)
            old_generation = 0
            if current_path.exists():
                old_generation = int(
                    json.loads(current_path.read_text(encoding="utf-8")).get("generation", 0)
                )
            promoted = {**manifest, "status": "promoted", "generation": old_generation + 1}
            _atomic_bytes(
                current_path,
                json.dumps(promoted, ensure_ascii=False, sort_keys=True, indent=2).encode(),
            )
        return DatasetArtifact(dataset, version, parquet_path, manifest_path, frame.height)

    @staticmethod
    def _verify_candidate_identity(
        dataset: str, version: str, manifest: dict[str, Any], manifest_path: Path
    ) -> None:
        required = {
            "checksum",
            "data_checksum",
            "schema_version",
            "source_checksums",
            "transformation_algorithm_version",
            "code_version",
            "configuration_hash",
            "git_commit",
            "dirty",
            "source_tree_hash",
        }
        if not required.issubset(manifest):
            raise RuntimeError(f"candidate manifest lacks provenance: {manifest_path}")
        identity = {key: manifest[key] for key in required if key not in {"checksum"}}
        identity["source_checksums"] = sorted(identity["source_checksums"])
        actual = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if (
            manifest["checksum"] != version
            or manifest.get("dataset") != dataset
            or actual != version
        ):
            raise RuntimeError(f"candidate identity mismatch: {manifest_path}")

    @staticmethod
    @contextmanager
    def _promotion_lock(directory: Path) -> Iterator[None]:
        directory.mkdir(parents=True, exist_ok=True)
        lock_path = directory / ".promotion.lock"
        with lock_path.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def read_dataset_version(self, dataset: str, version: str) -> pl.DataFrame:
        manifest_path = self.normalized_root / dataset / f"{version}.manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"dataset version not found: {dataset}@{version}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("checksum") != version:
            raise RuntimeError(f"dataset version mismatch: {manifest_path}")
        return self._read_verified(
            Path(manifest["parquet_path"]),
            str(manifest.get("data_checksum", manifest["checksum"])),
        )

    def current_version(self, dataset: str) -> str | None:
        pointer = self.normalized_root / dataset / "current.json"
        if not pointer.exists():
            pointer = self.normalized_root / dataset / "latest.json"
        if not pointer.exists():
            return None
        manifest = json.loads(pointer.read_text(encoding="utf-8"))
        return str(manifest["checksum"])

    def read_latest_dataset(self, dataset: str) -> pl.DataFrame | None:
        latest_path = self.normalized_root / dataset / "current.json"
        if not latest_path.exists():
            latest_path = self.normalized_root / dataset / "latest.json"
        if not latest_path.exists():
            return None
        manifest = json.loads(latest_path.read_text(encoding="utf-8"))
        path = Path(manifest["parquet_path"])
        if not path.exists():
            raise RuntimeError(f"normalized artifact is missing: {path}")
        return self._read_verified(path, str(manifest.get("data_checksum", manifest["checksum"])))

    @staticmethod
    def _read_verified(path: Path, expected_checksum: str) -> pl.DataFrame:
        if not path.exists():
            raise RuntimeError(f"normalized artifact is missing: {path}")
        frame = pl.read_parquet(path)
        actual = hashlib.sha256(_canonical_records(frame.sort(frame.columns))).hexdigest()
        if actual != expected_checksum:
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
