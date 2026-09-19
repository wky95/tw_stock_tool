from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from island_quant.backtest.artifacts import BacktestArtifactStore
from island_quant.dashboard.app import create_app
from island_quant.dashboard.backtests import BacktestArtifactQuery
from island_quant.dashboard.pipelines import PipelineArtifactQuery
from island_quant.dashboard.service import DashboardQueryService
from island_quant.pipeline.adapters import NormalizedMarketDataReader
from island_quant.pipeline.artifacts import ExactArtifactStore
from island_quant.pipeline.exploratory import (
    ExploratoryPipelineConfig,
    RealCacheExploratoryPipeline,
)
from island_quant.pipeline.orchestration import CheckpointedPipeline, ResourcePolicy
from island_quant.pipeline.predictions import (
    PredictionEligibility,
    SelectedOOSPredictionReader,
)

TAIPEI = ZoneInfo("Asia/Taipei")
HEX = "a" * 64


def _market_record(symbol: str = "2330") -> dict[str, object]:
    return {
        "instrument_id": f"TWSE:{symbol}",
        "market": "TWSE",
        "trade_date": "2025-01-02",
        "event_time": "2025-01-02T13:30:00+08:00",
        "available_at": "2025-01-02T18:00:00+08:00",
        "open": "100",
        "high": "102",
        "low": "99",
        "close": "101",
        "volume": 1000,
    }


def test_exact_store_is_deterministic_streaming_and_idempotent(tmp_path: Path) -> None:
    store = ExactArtifactStore(tmp_path, "normalized_market_data", 1)
    first = store.publish(
        (_market_record("2330"), _market_record("2317")),
        lineage={"source": HEX},
        completeness="incomplete",
        classification="real_exploratory",
        created_at=datetime(2025, 1, 1, tzinfo=TAIPEI),
        write_batch_size=1,
    )
    second = store.publish(
        (_market_record("2317"), _market_record("2330")),
        lineage={"source": HEX},
        completeness="incomplete",
        classification="real_exploratory",
        created_at=datetime(2026, 1, 1, tzinfo=TAIPEI),
        write_batch_size=50,
    )
    assert first.artifact_version == second.artifact_version
    batches = tuple(store.batches(first.artifact_version, batch_size=1))
    assert [len(batch.records) for batch in batches] == [1, 1]
    records = tuple(NormalizedMarketDataReader(tmp_path).read_batches(first.artifact_version))
    assert records[0][0].close == Decimal("101")


@pytest.mark.parametrize("version", ["", "latest", "current", "../bad", "/tmp/x", "A" * 64])
def test_exact_store_rejects_noncanonical_versions(tmp_path: Path, version: str) -> None:
    with pytest.raises(ValueError):
        ExactArtifactStore(tmp_path, "features", 1).manifest(version)


def test_exact_store_rejects_corruption_incomplete_and_symlink(tmp_path: Path) -> None:
    store = ExactArtifactStore(tmp_path, "features", 1)
    manifest = store.publish(
        ({"id": 1},),
        lineage={"source": HEX},
        completeness="incomplete",
        classification="real_exploratory",
        created_at=datetime(2025, 1, 1, tzinfo=TAIPEI),
    )
    directory = tmp_path / "features" / manifest.artifact_version
    (directory / "records.sha256").write_text("0" * 64, encoding="ascii")
    with pytest.raises(RuntimeError, match="checksum"):
        store.manifest(manifest.artifact_version)

    missing_root = tmp_path / "missing"
    target = missing_root / "features" / HEX
    target.mkdir(parents=True)
    (target / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="incomplete"):
        ExactArtifactStore(missing_root, "features", 1).manifest(HEX)

    symlink_root = tmp_path / "symlink"
    (symlink_root / "features").mkdir(parents=True)
    (symlink_root / "features" / HEX).symlink_to(directory)
    with pytest.raises((ValueError, RuntimeError)):
        ExactArtifactStore(symlink_root, "features", 1).manifest(HEX)


def _prediction_record(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "strategy_run": "run-v1",
        "experiment_id": "experiment-v1",
        "fold_id": "fold-v1",
        "model_artifact_version": "b" * 64,
        "feature_artifact_version": "c" * 64,
        "label_version": "label-v1",
        "dataset_version": "d" * 64,
        "universe_version": "e" * 64,
        "instrument_id": "TWSE:2330",
        "decision_time": "2025-01-02T18:00:00+08:00",
        "available_at": "2025-01-02T17:59:00+08:00",
        "fit_cutoff": "2025-01-01T18:00:00+08:00",
        "prediction": "0.01",
        "target_definition": "next-session-open-to-close-return",
        "split_role": "test",
        "completeness_status": "exploratory",
        "holdout_access_audit": None,
    }
    payload.update(changes)
    import hashlib

    from island_quant.pipeline.artifacts import canonical_json

    payload["checksum"] = hashlib.sha256(canonical_json({**payload, "checksum": ""})).hexdigest()
    return payload


def _prediction_artifact(tmp_path: Path, records: tuple[dict[str, object], ...]) -> str:
    manifest = ExactArtifactStore(tmp_path, "selected_oos_predictions", 1).publish(
        records,
        lineage={"dataset_version": "d" * 64, "universe_version": "e" * 64},
        completeness="incomplete",
        classification="real_exploratory",
        created_at=datetime(2025, 1, 2, tzinfo=TAIPEI),
    )
    return manifest.artifact_version


def _eligibility() -> PredictionEligibility:
    return PredictionEligibility(
        frozenset({("TWSE:2330", date(2025, 1, 2))}),
        {"TWSE:2330": (date(1994, 9, 5), None)},
        "d" * 64,
        "e" * 64,
    )


def test_selected_prediction_adapter_returns_typed_validated_ledger(tmp_path: Path) -> None:
    version = _prediction_artifact(tmp_path, (_prediction_record(),))
    ledger = SelectedOOSPredictionReader(tmp_path).read(version, _eligibility(), batch_size=1)
    assert ledger.version == version
    assert ledger.records[0].experiment_id == "experiment-v1"


@pytest.mark.parametrize(
    "change,match",
    [
        ({"split_role": "train"}, "in-sample"),
        ({"split_role": "validation"}, "validation"),
        ({"split_role": "final_holdout"}, "access audit"),
        ({"fit_cutoff": "2025-01-02T18:00:00+08:00"}, "fit cutoff"),
        ({"available_at": "2025-01-03T00:00:00+08:00"}, "unavailable"),
    ],
)
def test_selected_prediction_adapter_fails_closed(
    tmp_path: Path, change: dict[str, object], match: str
) -> None:
    version = _prediction_artifact(tmp_path, (_prediction_record(**change),))
    with pytest.raises(ValueError, match=match):
        SelectedOOSPredictionReader(tmp_path).read(version, _eligibility())


def test_selected_prediction_adapter_rejects_duplicate_and_noneligible(tmp_path: Path) -> None:
    record = _prediction_record()
    version = _prediction_artifact(tmp_path, (record, record))
    with pytest.raises(ValueError, match="duplicate"):
        SelectedOOSPredictionReader(tmp_path).read(version, _eligibility())
    version = _prediction_artifact(
        tmp_path / "other", (_prediction_record(instrument_id="TWSE:2317"),)
    )
    with pytest.raises(ValueError, match="eligible"):
        SelectedOOSPredictionReader(tmp_path / "other").read(version, _eligibility())


def test_resource_policy_requires_explicit_large_run_confirmation() -> None:
    policy = ResourcePolicy(maximum_instruments=2, maximum_date_range_days=10, maximum_rows=20)
    with pytest.raises(RuntimeError, match="confirm-large-run"):
        policy.check(3, date(2025, 1, 1), date(2025, 1, 3), 9, False)
    policy.check(3, date(2025, 1, 1), date(2025, 1, 3), 9, True)


def _copy_cache(source: Path, destination: Path, symbols: tuple[str, ...]) -> None:
    destination.mkdir()
    for symbol in symbols:
        candidate = sorted(source.glob(f"finmind_{symbol}_2020-*.json"))[0]
        shutil.copy(candidate, destination / candidate.name)


def test_real_cache_pipeline_is_checkpointed_deterministic_and_not_synthetic(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    symbols = ("1301", "2317", "2330")
    _copy_cache(Path("data/cache"), cache, symbols)
    config = ExploratoryPipelineConfig(
        cache,
        tmp_path / "artifacts",
        tmp_path / "state",
        symbols,
        date(2025, 9, 17),
        date(2025, 10, 31),
        chunk_size=7,
    )
    pipeline = RealCacheExploratoryPipeline(config)
    plan = pipeline.plan()
    assert plan.classification == "real_exploratory"
    first = pipeline.run()
    second = RealCacheExploratoryPipeline(config).run()
    assert first.run_version == second.run_version
    assert first.synthetic is second.synthetic is False
    assert all(stage.status == "completed" for stage in first.stages)
    assert all(stage.status == "completed" for stage in second.stages)
    assert "incomplete_point_in_time_reference_data" in first.coverage.promotion_blockers
    assert first.backtest_artifact_version is not None
    backtest = BacktestArtifactStore(config.artifact_root).read(first.backtest_artifact_version)
    assert backtest["manifest"]["synthetic_demo"] is False
    assert backtest["manifest"]["classification"] == "exploratory"
    assert backtest["reconciliation_report"]["status"] == "passed"
    payload = CheckpointedPipeline(config.artifact_root, config.state_root).inspect(
        first.run_version
    )
    assert payload["classification"] == "real_exploratory"

    query = PipelineArtifactQuery(config.artifact_root, first.run_version)
    backtest_query = BacktestArtifactQuery(
        BacktestArtifactStore(config.artifact_root), first.backtest_artifact_version
    )
    client = TestClient(
        create_app(
            DashboardQueryService(
                pipeline_query=query, backtest_query=backtest_query
            )
        )
    )
    response = client.get(f"/api/dashboard/pipelines/{first.run_version}")
    assert response.status_code == 200
    assert response.json()["mode"] == "pipeline"
    assert client.get("/health").json()["mode"] == "pipeline-read-only"
    page = client.get(f"/pipelines/{first.run_version}")
    assert "REAL EXPLORATORY" in page.text
    assert "Promotion blockers" in page.text
    detail = client.get(f"/backtests/{first.backtest_artifact_version}")
    assert detail.status_code == 200
    assert "PIPELINE" in detail.text
    detail_api = client.get(
        f"/api/dashboard/backtests/{first.backtest_artifact_version}"
    )
    assert detail_api.json()["mode"] == "pipeline"


def test_pipeline_dashboard_rejects_invalid_and_unknown_versions(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        PipelineArtifactQuery(tmp_path, "../bad")
    with pytest.raises(FileNotFoundError):
        PipelineArtifactQuery(tmp_path, HEX)


def test_pipeline_manifest_contains_no_absolute_paths(tmp_path: Path) -> None:
    coverage = {
        "date_coverage": "none",
        "instrument_coverage": "none",
        "unknown_market": 0,
        "provisional_listing_date": 0,
        "missing_delisted_history": True,
        "corporate_action_coverage": "missing",
        "suspension_coverage": "missing",
        "price_limit_tradability_coverage": "missing",
        "benchmark_coverage": "missing",
        "market_cap_coverage": "missing",
        "feature_validity": "missing",
        "label_validity": "missing",
        "prediction_coverage": "missing",
        "backtest_valuation_completeness": "missing",
        "pit_research_completeness": "incomplete",
        "promotion_blockers": ("missing_data",),
    }
    from island_quant.pipeline.coverage import CoverageReport

    report = CoverageReport.create(**coverage)
    report.verify()
    assert not any(str(tmp_path) in json.dumps(value, default=str) for value in (asdict(report),))
