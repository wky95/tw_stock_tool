"""Checkpointed, deterministic exact-version pipeline orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from island_quant.pipeline.artifacts import (
    ExactArtifactStore,
    canonical_json,
    require_exact_version,
)
from island_quant.pipeline.coverage import CoverageReport

STAGES = (
    "normalized_market_data",
    "pit_universe",
    "features",
    "labels",
    "walk_forward_ml",
    "selected_oos_predictions",
    "targets",
    "event_driven_backtest",
    "analytics_attribution",
    "immutable_artifacts",
)


@dataclass(frozen=True, slots=True)
class ResourcePolicy:
    chunk_size: int = 1000
    maximum_instruments: int = 50
    maximum_date_range_days: int = 730
    maximum_rows: int = 1_000_000

    def check(
        self, instruments: int, start: date, end: date, estimated_rows: int, confirmed: bool
    ) -> None:
        if (
            self.chunk_size < 1
            or min(self.maximum_instruments, self.maximum_date_range_days, self.maximum_rows) < 1
        ):
            raise ValueError("pipeline resource policy values must be positive")
        large = (
            instruments > self.maximum_instruments
            or (end - start).days + 1 > self.maximum_date_range_days
            or estimated_rows > self.maximum_rows
        )
        if large and not confirmed:
            raise RuntimeError("pipeline exceeds safety limits; pass --confirm-large-run")


@dataclass(frozen=True, slots=True)
class PipelinePlan:
    source: str
    instruments: tuple[str, ...]
    start: date
    end: date
    estimated_rows: int
    stages: tuple[str, ...]
    classification: str


@dataclass(frozen=True, slots=True)
class PipelineStageResult:
    name: str
    status: str
    output_version: str | None
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class PipelineRun:
    run_version: str
    config_checksum: str
    stages: tuple[PipelineStageResult, ...]
    coverage: CoverageReport
    backtest_artifact_version: str | None
    classification: str
    synthetic: bool
    status: str


class CheckpointedPipeline:
    def __init__(self, artifact_root: Path, state_root: Path) -> None:
        self.artifact_root = artifact_root
        self.state_root = state_root
        self.store = ExactArtifactStore(artifact_root, "pipeline_runs", 1)

    def execute(
        self,
        *,
        config: Mapping[str, object],
        coverage: CoverageReport,
        runners: Mapping[str, Callable[[dict[str, str]], str]],
        backtest_version: Callable[[dict[str, str]], str | None],
        validators: Mapping[str, Callable[[str], None]] | None = None,
    ) -> PipelineRun:
        config_checksum = hashlib.sha256(canonical_json(config)).hexdigest()
        checkpoint_dir = self.state_root / "pipeline" / config_checksum
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        outputs: dict[str, str] = {}
        stages: list[PipelineStageResult] = []
        for name in STAGES:
            checkpoint = checkpoint_dir / f"{name}.json"
            if checkpoint.exists():
                saved = json.loads(checkpoint.read_text(encoding="utf-8"))
                version = require_exact_version(str(saved["output_version"]))
                if validators is not None:
                    try:
                        validator = validators[name]
                    except KeyError as exc:
                        raise RuntimeError(f"missing resume validator for stage {name}") from exc
                    validator(version)
                outputs[name] = version
                stages.append(PipelineStageResult(name, "completed", version, None))
                continue
            try:
                version = require_exact_version(runners[name](dict(outputs)))
            except Exception as exc:
                reason = type(exc).__name__
                stages.append(PipelineStageResult(name, "failed", None, reason))
                return self._publish(config_checksum, tuple(stages), coverage, None, "failed")
            outputs[name] = version
            _atomic_json(checkpoint, {"stage": name, "output_version": version})
            stages.append(PipelineStageResult(name, "completed", version, None))
        backtest = backtest_version(outputs)
        return self._publish(config_checksum, tuple(stages), coverage, backtest, "completed")

    def inspect(self, version: str) -> dict[str, Any]:
        batches = tuple(self.store.batches(version, batch_size=1))
        if len(batches) != 1 or len(batches[0].records) != 1:
            raise RuntimeError("pipeline run artifact must contain exactly one record")
        return batches[0].records[0]

    def _publish(
        self,
        config_checksum: str,
        stages: tuple[PipelineStageResult, ...],
        coverage: CoverageReport,
        backtest_version: str | None,
        status: str,
    ) -> PipelineRun:
        record = {
            "config_checksum": config_checksum,
            "stages": [asdict(item) for item in stages],
            "coverage": asdict(coverage),
            "backtest_artifact_version": backtest_version,
            "classification": "real_exploratory",
            "synthetic": False,
            "status": status,
        }
        manifest = self.store.publish(
            (record,),
            lineage={"config_checksum": config_checksum},
            completeness=coverage.pit_research_completeness,
            classification="real_exploratory",
            created_at=datetime.now(UTC),
        )
        return PipelineRun(
            manifest.artifact_version,
            config_checksum,
            stages,
            coverage,
            backtest_version,
            "real_exploratory",
            False,
            status,
        )


def _atomic_json(path: Path, value: object) -> None:
    content = canonical_json(value)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
