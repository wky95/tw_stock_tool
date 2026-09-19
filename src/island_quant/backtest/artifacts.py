"""Immutable content-addressed candidate backtest artifacts and exact-version reader."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from island_quant.analytics.performance import (
    AttributionReport,
    BenchmarkReport,
    PerformanceReport,
)
from island_quant.analytics.scenarios import ScenarioOutcome
from island_quant.backtest.engine import BacktestResult
from island_quant.storage.provenance import CodeProvenance, capture_code_provenance

VERSION_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def validate_artifact_version(version: str) -> str:
    """Validate an opaque SHA-256 identity before any filesystem operation."""
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("artifact version must be a 64-character lowercase hex digest")
    return version


@dataclass(frozen=True, slots=True)
class BacktestArtifactManifest:
    artifact_version: str
    backtest_run_id: str
    strategy_version: str
    target_policy_version: str
    prediction_artifact_version: str
    model_artifact_version: str
    dataset_version: str
    universe_version: str
    calendar_version: str
    corporate_action_version: str
    benchmark_version: str
    fee_tax_policy_version: str
    settlement_policy_version: str
    execution_model_version: str
    risk_policy_version: str
    mark_policy_version: str
    initial_capital: Decimal
    date_range: tuple[str, str]
    seed: int
    git_commit: str
    dirty: bool
    source_tree_hash: str
    config_hash: str
    input_checksums: tuple[tuple[str, str], ...]
    event_journal_checksum: str
    ledger_checksum: str
    snapshot_checksum: str
    report_checksum: str
    completeness_status: str
    synthetic_demo: bool
    classification: str
    created_time: datetime
    scenario_runner_version: str
    scenario_count: int
    scenario_config_checksums: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BacktestArtifact:
    manifest: BacktestArtifactManifest
    document: dict[str, Any]


class BacktestArtifactStore:
    def __init__(
        self,
        root: Path,
        provenance_provider: Callable[[], CodeProvenance] = capture_code_provenance,
    ) -> None:
        self.root = root
        self.provenance_provider = provenance_provider

    def build(
        self,
        result: BacktestResult,
        performance: PerformanceReport,
        attribution: AttributionReport,
        scenarios: tuple[ScenarioOutcome, ...],
        *,
        target_policy_version: str,
        prediction_artifact_version: str,
        model_artifact_version: str,
        dataset_version: str,
        universe_version: str,
        calendar_version: str,
        corporate_action_version: str,
        benchmark_version: str,
        benchmark: BenchmarkReport | None,
        seed: int,
        created_time: datetime,
        completeness_status: str,
        synthetic_demo: bool,
        config: dict[str, Any],
        input_checksums: dict[str, str],
    ) -> BacktestArtifact:
        versions = {
            target_policy_version,
            prediction_artifact_version,
            model_artifact_version,
            dataset_version,
            universe_version,
            calendar_version,
            corporate_action_version,
            benchmark_version,
        }
        if versions & {"", "latest", "current"}:
            raise ValueError("backtest artifacts require exact pinned input versions")
        if created_time.tzinfo is None or created_time.utcoffset() is None:
            raise ValueError("artifact creation time must be timezone-aware")
        provenance = self.provenance_provider()
        config_hash = hashlib.sha256(_canonical(config)).hexdigest()
        classification = (
            "exploratory"
            if synthetic_demo or completeness_status != "validated" or provenance.dirty
            else "candidate"
        )
        report_checksum = hashlib.sha256(_canonical(asdict(performance))).hexdigest()
        manifest = BacktestArtifactManifest(
            "",
            result.config.run_id,
            result.strategy_version,
            target_policy_version,
            prediction_artifact_version,
            model_artifact_version,
            dataset_version,
            universe_version,
            calendar_version,
            corporate_action_version,
            benchmark_version,
            result.config.fee_policy.version,
            result.config.settlement_policy.version,
            result.config.fill_policy.version,
            result.config.risk_policy.version,
            result.config.mark_policy.version,
            result.config.initial_cash,
            (
                result.snapshots[0].as_of.date().isoformat(),
                result.final_snapshot.as_of.date().isoformat(),
            ),
            seed,
            provenance.git_commit,
            provenance.dirty,
            provenance.source_tree_hash,
            config_hash,
            tuple(sorted(input_checksums.items())),
            result.event_checksum,
            result.journal_checksum,
            result.final_snapshot.checksum,
            report_checksum,
            completeness_status,
            synthetic_demo,
            classification,
            created_time,
            "isolated-sensitivity-grid-v1",
            len(scenarios),
            tuple(item.config.checksum for item in scenarios),
        )
        document = {
            "manifest": asdict(manifest),
            "config": config,
            "event_journal": [asdict(item) for item in result.events],
            "daily_snapshots": [asdict(item) for item in result.snapshots],
            "final_snapshot": asdict(result.final_snapshot),
            "orders": _events(result, "order_intent.created"),
            "fills": _events(result, "fill.received"),
            "risk_decisions": _events(result, "risk.decided"),
            "performance_metrics": asdict(performance),
            "drawdown_series": [asdict(item) for item in performance.drawdown_series],
            "benchmark_series": asdict(benchmark) if benchmark else None,
            "attribution": asdict(attribution),
            "sensitivity_results": [asdict(item) for item in scenarios],
            "warnings": _artifact_warnings(
                synthetic_demo=synthetic_demo,
                completeness_status=completeness_status,
                dirty=provenance.dirty,
            ),
            "reconciliation_report": {
                "status": "passed"
                if performance.reconciliation_residual == 0
                else "failed",
                "residual": performance.reconciliation_residual,
            },
        }
        version = hashlib.sha256(_canonical(_identity_payload(document))).hexdigest()
        manifest = replace(manifest, artifact_version=version)
        document["manifest"] = asdict(manifest)
        return BacktestArtifact(manifest, document)

    def save(self, artifact: BacktestArtifact) -> Path:
        self._verify(artifact.document, artifact.manifest.artifact_version)
        version = validate_artifact_version(artifact.manifest.artifact_version)
        base = self._base()
        published = self._safe_version_directory(version)
        path = published / "artifact.json"
        content = _canonical(artifact.document)
        if published.exists():
            self._assert_idempotent(version, artifact.document)
            return path
        base.mkdir(parents=True, exist_ok=True)
        candidate = Path(tempfile.mkdtemp(dir=base, prefix=".candidate-"))
        try:
            _write_fsynced(candidate / "artifact.json", content)
            digest = hashlib.sha256(content).hexdigest().encode()
            _write_fsynced(candidate / "artifact.sha256", digest)
            _fsync_directory(candidate)
            self._verify_candidate(candidate, version)
            try:
                os.rename(candidate, published)
                _fsync_directory(base)
            except OSError:
                if not published.exists():
                    raise
                self._assert_idempotent(version, artifact.document)
        finally:
            if candidate.exists():
                shutil.rmtree(candidate)
        return path

    def read(self, version: str) -> dict[str, Any]:
        validate_artifact_version(version)
        directory = self._safe_version_directory(version)
        path = directory / "artifact.json"
        checksum_path = directory / "artifact.sha256"
        if not directory.is_dir() or not path.is_file() or not checksum_path.is_file():
            raise FileNotFoundError("backtest artifact not found")
        if directory.is_symlink() or path.is_symlink() or checksum_path.is_symlink():
            raise RuntimeError("backtest artifact symlinks are forbidden")
        content = path.read_bytes()
        expected = checksum_path.read_text(encoding="ascii")
        if hashlib.sha256(content).hexdigest() != expected:
            raise RuntimeError("backtest artifact file checksum mismatch")
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise RuntimeError("backtest artifact root must be an object")
        self._verify(payload, version)
        return payload

    def assert_validated_promotion_allowed(self, version: str) -> None:
        artifact = self.read(version)
        manifest = artifact["manifest"]
        reasons: list[str] = []
        if manifest["dirty"]:
            reasons.append("dirty_worktree")
        if manifest["completeness_status"] != "validated":
            reasons.append("incomplete_pit")
        if manifest["synthetic_demo"]:
            reasons.append("synthetic_demo")
        if Decimal(str(artifact["reconciliation_report"]["residual"])) != 0:
            reasons.append("nonzero_reconciliation_residual")
        reasons.append("slice2_candidate_only")
        raise RuntimeError(f"validated promotion rejected: {','.join(reasons)}")

    @staticmethod
    def _verify(payload: dict[str, Any], version: str) -> None:
        validate_artifact_version(version)
        try:
            manifest = dict(payload["manifest"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("backtest artifact manifest is incomplete") from exc
        required_manifest = set(BacktestArtifactManifest.__dataclass_fields__)
        required_document = {
            "config",
            "event_journal",
            "daily_snapshots",
            "final_snapshot",
            "orders",
            "fills",
            "risk_decisions",
            "performance_metrics",
            "drawdown_series",
            "benchmark_series",
            "attribution",
            "sensitivity_results",
            "warnings",
            "reconciliation_report",
        }
        if not required_manifest.issubset(manifest) or not required_document.issubset(payload):
            raise RuntimeError("backtest artifact manifest is incomplete")
        if manifest.get("artifact_version") != version:
            raise RuntimeError("backtest artifact version mismatch")
        if hashlib.sha256(_canonical(_identity_payload(payload))).hexdigest() != version:
            raise RuntimeError("backtest artifact checksum mismatch")
        if hashlib.sha256(_canonical(payload["performance_metrics"])).hexdigest() != manifest.get(
            "report_checksum"
        ):
            raise RuntimeError("backtest performance checksum mismatch")
        if hashlib.sha256(_canonical(payload["event_journal"])).hexdigest() != manifest.get(
            "event_journal_checksum"
        ):
            raise RuntimeError("backtest event journal checksum mismatch")
        scenario_results = payload["sensitivity_results"]
        if not isinstance(scenario_results, list):
            raise RuntimeError("backtest scenario inventory is invalid")
        scenario_checksums = tuple(
            str(item["config"]["checksum"])
            if "checksum" in item["config"]
            else hashlib.sha256(_canonical(item["config"])).hexdigest()
            for item in scenario_results
        )
        if int(manifest["scenario_count"]) != len(scenario_results) or tuple(
            manifest["scenario_config_checksums"]
        ) != scenario_checksums:
            raise RuntimeError("backtest scenario manifest mismatch")
        final_snapshot = payload["final_snapshot"]
        if final_snapshot.get("checksum") != manifest.get("snapshot_checksum"):
            raise RuntimeError("backtest final snapshot checksum mismatch")
        snapshot_body = {
            key: value for key, value in final_snapshot.items() if key != "checksum"
        }
        if hashlib.sha256(_canonical(snapshot_body)).hexdigest() != manifest.get(
            "snapshot_checksum"
        ):
            raise RuntimeError("backtest final snapshot content mismatch")

    def _base(self) -> Path:
        base = (self.root / "backtests").resolve()
        root = self.root.resolve()
        if not base.is_relative_to(root):
            raise RuntimeError("configured artifact location escapes its root")
        return base

    def _safe_version_directory(self, version: str) -> Path:
        validate_artifact_version(version)
        base = self._base()
        target = base / version
        resolved = target.resolve()
        if not resolved.is_relative_to(base):
            raise RuntimeError("artifact version escapes the configured root")
        if target.is_symlink():
            raise RuntimeError("backtest artifact symlinks are forbidden")
        return target

    def _verify_candidate(self, candidate: Path, version: str) -> None:
        content = (candidate / "artifact.json").read_bytes()
        expected = (candidate / "artifact.sha256").read_text(encoding="ascii")
        if hashlib.sha256(content).hexdigest() != expected:
            raise RuntimeError("candidate artifact checksum mismatch")
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise RuntimeError("candidate artifact root must be an object")
        self._verify(payload, version)

    def _assert_idempotent(self, version: str, incoming: dict[str, Any]) -> None:
        existing = self.read(version)
        if _canonical(_identity_payload(existing)) != _canonical(
            _identity_payload(incoming)
        ):
            raise RuntimeError("immutable backtest artifact collision")


def _events(result: BacktestResult, kind: str) -> list[dict[str, Any]]:
    return [asdict(item) for item in result.events if item.kind.value == kind]


def _canonical(payload: object) -> bytes:
    return json.dumps(
        _normalize(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _normalize(payload: object) -> object:
    if isinstance(payload, Decimal):
        return str(payload)
    if isinstance(payload, (date, datetime)):
        return payload.isoformat()
    if isinstance(payload, StrEnum):
        return payload.value
    if isinstance(payload, dict):
        return {str(key): _normalize(value) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_normalize(value) for value in payload]
    return payload


def _identity_payload(payload: dict[str, Any]) -> dict[str, Any]:
    manifest = dict(payload["manifest"])
    manifest["artifact_version"] = ""
    manifest.pop("created_time", None)
    return {**payload, "manifest": manifest}


def _artifact_warnings(
    *, synthetic_demo: bool, completeness_status: str, dirty: bool
) -> list[str]:
    warnings = ["CANDIDATE RESEARCH OUTPUT — NOT FOR LIVE TRADING"]
    if synthetic_demo:
        warnings.append("SYNTHETIC ENGINEERING FIXTURE — NOT INVESTMENT EVIDENCE")
    if completeness_status != "validated":
        warnings.append("EXPLORATORY — INCOMPLETE POINT-IN-TIME DATA")
    if dirty:
        warnings.append("DIRTY SOURCE TREE — RESULT IS NOT PROMOTION ELIGIBLE")
    return warnings


def _write_fsynced(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
