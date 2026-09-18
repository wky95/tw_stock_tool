"""Checksum-verified JSON artifacts for Slice 4 experiments and model cards."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from island_quant.ml.dataset import SupervisedDatasetManifest
from island_quant.ml.experiment import ExperimentResult


def canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode()


def write_supervised_manifest(
    root: Path, manifest: SupervisedDatasetManifest, artifact_version: str | None = None
) -> Path:
    path = root / "ml_datasets" / f"{artifact_version or manifest.checksum}.json"
    content = canonical_json(asdict(manifest))
    _write_immutable(path, content)
    _write_immutable(path.with_suffix(".sha256"), hashlib.sha256(content).hexdigest().encode())
    return path


def read_supervised_manifest(root: Path, artifact_version: str) -> dict[str, Any]:
    if artifact_version in {"", "latest", "current"}:
        raise ValueError("an explicit supervised artifact version is required")
    path = root / "ml_datasets" / f"{artifact_version}.json"
    expected = path.with_suffix(".sha256").read_text(encoding="utf-8")
    return read_verified_json(path, expected)


def read_verified_json(path: Path, expected_checksum: str | None = None) -> dict[str, Any]:
    content = path.read_bytes()
    if expected_checksum is not None and hashlib.sha256(content).hexdigest() != expected_checksum:
        raise RuntimeError(f"artifact checksum mismatch: {path}")
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise RuntimeError(f"artifact root must be an object: {path}")
    return payload


class ExperimentStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, result: ExperimentResult) -> tuple[str, Path]:
        payload = {
            "experiment_id": result.experiment_id,
            "prediction_ledger": result.prediction_ledger.frame.to_dicts(),
            "model_artifacts": [asdict(item) for item in result.model_artifacts],
            "experiment_inventory": result.experiment_inventory,
            "aggregate_metrics": result.aggregate_metrics,
            "split_manifest": result.split_manifest,
            "final_holdout_access_count": result.final_holdout_access_count,
        }
        content = canonical_json(payload)
        checksum = hashlib.sha256(content).hexdigest()
        path = self.root / "ml_experiments" / f"{checksum}.json"
        _write_immutable(path, content)
        return checksum, path

    def inspect(self, version: str) -> dict[str, Any]:
        if version in {"", "latest", "current"}:
            raise ValueError("an explicit experiment artifact version is required")
        path = self.root / "ml_experiments" / f"{version}.json"
        return read_verified_json(path, version)

    def write_model_card(self, version: str, output: Path) -> str:
        experiment = self.inspect(version)
        cards = [item["model_card"] for item in experiment["model_artifacts"]]
        content = canonical_json(
            {
                "experiment_id": experiment["experiment_id"],
                "experiment_artifact_version": version,
                "model_cards": cards,
            }
        )
        checksum = hashlib.sha256(content).hexdigest()
        _atomic_write(output, content)
        return checksum


def _write_immutable(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise RuntimeError(f"immutable artifact collision: {path}")
        return
    _atomic_write(path, content)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
