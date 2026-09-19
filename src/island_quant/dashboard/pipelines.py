"""Read-only exact-version pipeline projection for the Dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from island_quant.pipeline.artifacts import ExactArtifactStore


class PipelineArtifactQuery:
    def __init__(self, root: Path, version: str) -> None:
        self.version = version
        self.store = ExactArtifactStore(root, "pipeline_runs", 1)
        batches = tuple(self.store.batches(version, batch_size=1))
        if len(batches) != 1 or len(batches[0].records) != 1:
            raise RuntimeError("pipeline artifact must contain exactly one record")
        self._record = batches[0].records[0]

    def status(self) -> dict[str, Any]:
        record = self._record
        return {
            "mode": "pipeline",
            "run_version": self.version,
            "classification": record["classification"],
            "synthetic": record["synthetic"],
            "status": record["status"],
            "stages": record["stages"],
            "coverage": record["coverage"],
            "backtest_artifact_version": record["backtest_artifact_version"],
        }
