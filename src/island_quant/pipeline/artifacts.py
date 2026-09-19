"""Secure, immutable, streaming JSONL artifacts for application adapters."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar

VERSION_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode()


def require_exact_version(version: str) -> str:
    if not VERSION_RE.fullmatch(version):
        raise ValueError("artifact version must be a canonical lowercase SHA-256 digest")
    return version


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    artifact_type: str
    schema_version: int
    artifact_version: str
    record_count: int
    batch_count: int
    lineage: tuple[tuple[str, str], ...]
    data_checksum: str
    completeness: str
    classification: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ArtifactBatch:
    index: int
    records: tuple[dict[str, Any], ...]


class ArtifactReader(Protocol):
    def manifest(self, version: str) -> ArtifactManifest: ...

    def batches(self, version: str, *, batch_size: int = 1000) -> Iterator[ArtifactBatch]: ...


class ExactArtifactStore:
    """CAS-published JSONL artifacts with constrained exact-version paths."""

    def __init__(self, root: Path, artifact_type: str, schema_version: int) -> None:
        if not artifact_type or "/" in artifact_type or "\\" in artifact_type:
            raise ValueError("artifact type must be a safe namespace")
        if schema_version < 1:
            raise ValueError("schema version must be positive")
        self.root = root
        self.artifact_type = artifact_type
        self.schema_version = schema_version

    def publish(
        self,
        records: Iterable[Mapping[str, object]],
        *,
        lineage: Mapping[str, str],
        completeness: str,
        classification: str,
        created_at: datetime,
        write_batch_size: int = 1000,
    ) -> ArtifactManifest:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("artifact creation timestamp must be timezone-aware")
        if write_batch_size < 1:
            raise ValueError("write batch size must be positive")
        pinned = tuple(
            sorted((str(key), require_exact_version(value)) for key, value in lineage.items())
        )
        normalized = [dict(item) for item in records]
        normalized.sort(key=canonical_json)
        content = b"".join(canonical_json(item) + b"\n" for item in normalized)
        data_checksum = hashlib.sha256(content).hexdigest()
        identity = {
            "artifact_type": self.artifact_type,
            "schema_version": self.schema_version,
            "record_count": len(normalized),
            "lineage": pinned,
            "data_checksum": data_checksum,
            "completeness": completeness,
            "classification": classification,
        }
        version = hashlib.sha256(canonical_json(identity)).hexdigest()
        batch_count = 1 if normalized else 0
        manifest = ArtifactManifest(
            self.artifact_type,
            self.schema_version,
            version,
            len(normalized),
            batch_count,
            pinned,
            data_checksum,
            completeness,
            classification,
            created_at,
        )
        destination = self._directory(version)
        if destination.exists():
            existing = self.manifest(version)
            if _manifest_identity(existing) != _manifest_identity(manifest):
                raise RuntimeError("immutable artifact identity collision")
            return existing
        base = self._base()
        base.mkdir(parents=True, exist_ok=True)
        candidate = Path(tempfile.mkdtemp(prefix=".candidate-", dir=base))
        try:
            _write_fsynced(candidate / "records.jsonl", content)
            _write_fsynced(candidate / "records.sha256", data_checksum.encode("ascii"))
            _write_fsynced(candidate / "manifest.json", canonical_json(asdict(manifest)))
            self._verify_directory(candidate, version)
            try:
                os.rename(candidate, destination)
                _fsync(base)
            except OSError:
                if not destination.exists():
                    raise
                existing = self.manifest(version)
                if _manifest_identity(existing) != _manifest_identity(manifest):
                    raise RuntimeError("concurrent artifact identity collision") from None
        finally:
            if candidate.exists():
                shutil.rmtree(candidate)
        return manifest

    def manifest(self, version: str) -> ArtifactManifest:
        directory = self._directory(require_exact_version(version))
        self._verify_directory(directory, version)
        payload = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        try:
            result = ArtifactManifest(
                artifact_type=str(payload["artifact_type"]),
                schema_version=int(payload["schema_version"]),
                artifact_version=str(payload["artifact_version"]),
                record_count=int(payload["record_count"]),
                batch_count=int(payload["batch_count"]),
                lineage=tuple((str(k), str(v)) for k, v in payload["lineage"]),
                data_checksum=str(payload["data_checksum"]),
                completeness=str(payload["completeness"]),
                classification=str(payload["classification"]),
                created_at=datetime.fromisoformat(str(payload["created_at"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("artifact manifest is incomplete") from exc
        if (
            result.artifact_type != self.artifact_type
            or result.schema_version != self.schema_version
        ):
            raise RuntimeError("artifact type or schema version mismatch")
        if result.artifact_version != version:
            raise RuntimeError("artifact version mismatch")
        for _, pinned in result.lineage:
            require_exact_version(pinned)
        return result

    def batches(self, version: str, *, batch_size: int = 1000) -> Iterator[ArtifactBatch]:
        if batch_size < 1:
            raise ValueError("batch size must be positive")
        manifest = self.manifest(version)
        path = self._directory(version) / "records.jsonl"
        records: list[dict[str, Any]] = []
        index = 0
        seen = 0
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise RuntimeError("artifact record must be an object")
                records.append(item)
                seen += 1
                if len(records) == batch_size:
                    yield ArtifactBatch(index, tuple(records))
                    records = []
                    index += 1
        if records:
            yield ArtifactBatch(index, tuple(records))
        if seen != manifest.record_count:
            raise RuntimeError("artifact record count mismatch")

    def _base(self) -> Path:
        return self.root / self.artifact_type

    def _directory(self, version: str) -> Path:
        require_exact_version(version)
        base = self._base().resolve()
        candidate = (base / version).resolve(strict=False)
        if candidate.parent != base:
            raise ValueError("artifact path escapes configured root")
        return candidate

    def _verify_directory(self, directory: Path, version: str) -> None:
        if not directory.is_dir():
            raise FileNotFoundError("artifact not found")
        manifest_path = directory / "manifest.json"
        records_path = directory / "records.jsonl"
        checksum_path = directory / "records.sha256"
        if directory.is_symlink() or any(
            path.is_symlink() for path in (manifest_path, records_path, checksum_path)
        ):
            raise RuntimeError("artifact symlinks are forbidden")
        if not all(path.is_file() for path in (manifest_path, records_path, checksum_path)):
            raise RuntimeError("artifact is incomplete")
        digest = hashlib.sha256()
        record_count = 0
        with records_path.open("rb") as stream:
            for line in stream:
                digest.update(line)
                if line.strip():
                    record_count += 1
        actual = digest.hexdigest()
        if checksum_path.read_text(encoding="ascii") != actual:
            raise RuntimeError("artifact data checksum mismatch")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_batches = 1 if record_count else 0
        if (
            payload.get("record_count") != record_count
            or payload.get("batch_count") != expected_batches
        ):
            raise RuntimeError("artifact record inventory mismatch")
        if payload.get("artifact_version") != version or payload.get("data_checksum") != actual:
            raise RuntimeError("artifact manifest checksum mismatch")
        identity = {key: payload[key] for key in _IDENTITY_FIELDS}
        if hashlib.sha256(canonical_json(identity)).hexdigest() != version:
            raise RuntimeError("artifact content identity mismatch")


_IDENTITY_FIELDS = (
    "artifact_type",
    "schema_version",
    "record_count",
    "lineage",
    "data_checksum",
    "completeness",
    "classification",
)


def _manifest_identity(manifest: ArtifactManifest) -> tuple[object, ...]:
    return tuple(getattr(manifest, field) for field in _IDENTITY_FIELDS)


def _write_fsynced(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


T = TypeVar("T", covariant=True)


class TypedArtifactAdapter(Protocol[T]):
    def read_batches(self, version: str, *, batch_size: int = 1000) -> Iterator[tuple[T, ...]]: ...
