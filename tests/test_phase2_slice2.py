from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from island_quant.analytics.performance import (
    BenchmarkSeries,
    PerformanceAnalyzer,
    PerformancePolicy,
    _drawdowns,
    analyze_benchmark,
    attribute,
)
from island_quant.analytics.scenarios import ScenarioConfig, ScenarioRunner
from island_quant.backtest.artifacts import BacktestArtifact, BacktestArtifactStore
from island_quant.backtest.engine import EventDrivenBacktestEngine
from island_quant.backtest.fixtures import synthetic_backtest_fixture
from island_quant.backtest.integration import (
    _prediction_ledger,
    _strategy,
    build_demo_backtest_artifact,
)
from island_quant.backtest.predictions import (
    OOSPrediction,
    PredictionRole,
    SelectedPredictionLedger,
)
from island_quant.backtest.targets import (
    CoverageFailureAction,
    MissingPredictionAction,
    PredictionTargetBuilder,
    PredictionTargetPolicy,
    RebalanceFrequency,
    RebalancePolicy,
    TargetPolicyKind,
)
from island_quant.cli import main
from island_quant.dashboard.app import create_app
from island_quant.dashboard.backtests import BacktestArtifactQuery
from island_quant.dashboard.service import DashboardQueryService
from island_quant.portfolio.accounting import CashDividend, StockSplit
from island_quant.strategies.fixtures import EqualWeightFixtureStrategy

TAIPEI = ZoneInfo("Asia/Taipei")


def moment(day: date, hour: int = 18) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=TAIPEI)


def prediction(**changes: object) -> OOSPrediction:
    decision = moment(date(2024, 1, 2))
    values: dict[str, object] = {
        "strategy_run": "strategy-run-v1",
        "experiment_id": "experiment-v1",
        "fold_id": "fold-1",
        "model_artifact_version": "model-v1",
        "feature_artifact_version": "features-v1",
        "label_version": "label-v1",
        "prediction_artifact_version": "predictions-v1",
        "instrument_id": "TWSE:2330",
        "decision_time": decision,
        "available_at": decision - timedelta(minutes=1),
        "prediction": Decimal("0.1"),
        "target_definition": "next-open-return",
        "split_role": PredictionRole.TEST,
        "fit_cutoff": decision - timedelta(days=1),
        "completeness_status": "validated",
    }
    values.update(changes)
    return OOSPrediction.create(**values)


def ledger(records: tuple[OOSPrediction, ...], **changes: object) -> SelectedPredictionLedger:
    membership = {(item.instrument_id, item.decision_time.date()) for item in records}
    boundaries = {
        item.instrument_id: (date(2000, 1, 1), None) for item in records
    }
    values = {
        "eligible_membership": membership,
        "listing_boundaries": boundaries,
    }
    values.update(changes)
    return SelectedPredictionLedger.build("predictions-v1", records, **values)  # type: ignore[arg-type]


def test_prediction_timing_versions_and_checksum_fail_closed() -> None:
    with pytest.raises(ValueError, match="fit cutoff"):
        prediction(fit_cutoff=moment(date(2024, 1, 2)))
    with pytest.raises(ValueError, match="unavailable"):
        prediction(available_at=moment(date(2024, 1, 3)))
    with pytest.raises(ValueError, match="exact pinned"):
        prediction(model_artifact_version="latest")
    with pytest.raises(ValueError, match="checksum"):
        replace(prediction(), prediction=Decimal("9"))


def test_selected_oos_ownership_roles_fold_and_holdout_rules() -> None:
    first = prediction()
    with pytest.raises(ValueError, match="duplicate"):
        ledger((first, first))
    with pytest.raises(ValueError, match="in-sample"):
        ledger((prediction(split_role=PredictionRole.TRAIN),))
    second = prediction(instrument_id="TWSE:2317", fold_id="fold-2")
    with pytest.raises(ValueError, match="overlapping"):
        ledger((first, second))
    with pytest.raises(ValueError, match="access audit"):
        ledger((prediction(split_role=PredictionRole.FINAL_HOLDOUT),))
    mixed = prediction(instrument_id="TWSE:2317", model_artifact_version="model-v2")
    with pytest.raises(ValueError, match="mixed candidate-model"):
        ledger((first, mixed))


def test_validation_is_engineering_only_and_eligibility_boundaries_are_pinned() -> None:
    item = prediction(split_role=PredictionRole.VALIDATION)
    with pytest.raises(ValueError, match="formal OOS"):
        ledger((item,))
    selected = ledger((item,), allow_validation_fixture=True)
    assert selected.engineering_validation is True
    with pytest.raises(ValueError, match="eligible"):
        ledger((prediction(),), eligible_membership=set())
    with pytest.raises(ValueError, match="listing"):
        ledger(
            (prediction(),),
            listing_boundaries={"TWSE:2330": (date(2025, 1, 1), None)},
        )


def test_top_k_quantile_threshold_and_ties_are_deterministic() -> None:
    fixture = synthetic_backtest_fixture()
    instruments = {item.key: item for item in fixture.instruments}
    decision = fixture.sessions[0].decision_time
    rows = tuple(
        prediction(
            instrument_id=item.key,
            decision_time=decision,
            available_at=decision - timedelta(minutes=1),
            fit_cutoff=decision - timedelta(days=1),
            prediction=Decimal("1") if index < 2 else None,
        )
        for index, item in enumerate(reversed(fixture.instruments))
    )
    builder = PredictionTargetBuilder()
    policy = PredictionTargetPolicy("top-v1", TargetPolicyKind.TOP_K, top_k=1)
    artifact, targets = builder.build(
        rows,
        instruments,
        strategy_version="strategy-v1",
        universe_version="universe-v1",
        policy=policy,
    )
    selected = [item.instrument_id for item in artifact.members if item.target_weight > 0]
    assert selected == [sorted(item.instrument_id for item in rows[:2])[0]]
    assert sum((target.target_weight for target in targets), Decimal("0")) <= Decimal("1")
    for kind in (TargetPolicyKind.QUANTILE, TargetPolicyKind.THRESHOLD):
        result, _ = builder.build(
            rows,
            instruments,
            strategy_version="strategy-v1",
            universe_version="universe-v1",
            policy=PredictionTargetPolicy(f"{kind}-v1", kind, threshold=Decimal("0")),
        )
        assert result.checksum


def test_non_registry_and_missing_predictions_are_never_selected() -> None:
    fixture = synthetic_backtest_fixture()
    row = prediction(instrument_id="TWSE:9999", prediction=None)
    artifact, targets = PredictionTargetBuilder().build(
        (row,),
        {item.key: item for item in fixture.instruments},
        strategy_version="strategy-v1",
        universe_version="universe-v1",
        policy=PredictionTargetPolicy("top-v1", TargetPolicyKind.TOP_K, top_k=1),
    )
    assert targets == ()
    assert artifact.members[0].exclusion_reason == "data_quality:ineligible_instrument"


def test_rebalance_schedules_use_sessions_and_missing_policy_is_explicit() -> None:
    calendar = (
        date(2024, 1, 5),
        date(2024, 1, 8),
        date(2024, 1, 9),
        date(2024, 1, 15),
    )
    weekly = RebalancePolicy("weekly-v1", RebalanceFrequency.WEEKLY)
    every_two = RebalancePolicy("two-v1", RebalanceFrequency.N_SESSIONS, 2)
    assert weekly.scheduled_sessions(calendar) == (calendar[0], calendar[1], calendar[3])
    assert every_two.scheduled_sessions(calendar) == (calendar[0], calendar[2])
    assert RebalancePolicy("default-v1", RebalanceFrequency.DAILY).missing_prediction_action is (
        MissingPredictionAction.LIQUIDATE_TO_CASH
    )


def test_missing_decision_date_obeys_coverage_gate_before_liquidation() -> None:
    fixture = synthetic_backtest_fixture()
    strategy = _strategy(
        fixture,
        _prediction_ledger(fixture),
        PredictionTargetPolicy("top-v1", TargetPolicyKind.TOP_K, top_k=2),
        RebalancePolicy("daily-v1", RebalanceFrequency.DAILY),
    )
    first = fixture.sessions[0]
    missing = replace(
        strategy,
        artifacts=tuple(
            item for item in strategy.artifacts if item.decision_time != first.decision_time
        ),
        targets_by_decision=tuple(
            item for item in strategy.targets_by_decision if item[0] != first.decision_time
        ),
    )
    with pytest.raises(ValueError, match="coverage below"):
        missing.targets(first)

    skip = replace(
        missing,
        rebalance_policy=replace(
            missing.rebalance_policy,
            coverage_failure_action=CoverageFailureAction.SKIP_REBALANCE,
        ),
    )
    assert skip.targets(first) == ()


def test_skip_rebalance_is_distinct_from_hold_when_one_artifact_row_is_missing() -> None:
    fixture = synthetic_backtest_fixture()
    strategy = _strategy(
        fixture,
        _prediction_ledger(fixture),
        PredictionTargetPolicy("top-v1", TargetPolicyKind.TOP_K, top_k=2),
        RebalancePolicy(
            "skip-v1",
            RebalanceFrequency.DAILY,
            missing_prediction_action=MissingPredictionAction.SKIP_REBALANCE,
            minimum_prediction_coverage=Decimal("0"),
        ),
    )
    first_time, first_targets = strategy.targets_by_decision[0]
    incomplete = replace(
        strategy,
        targets_by_decision=((first_time, first_targets[:-1]), *strategy.targets_by_decision[1:]),
    )
    assert incomplete.targets(fixture.sessions[0]) == ()


def test_non_finite_predictions_and_mixed_target_inputs_fail_closed() -> None:
    with pytest.raises(ValueError, match="finite"):
        prediction(prediction=Decimal("NaN"))
    fixture = synthetic_backtest_fixture()
    decision = fixture.sessions[0].decision_time
    rows = (
        prediction(
            decision_time=decision,
            available_at=decision - timedelta(minutes=1),
            fit_cutoff=decision - timedelta(days=1),
        ),
        prediction(
            instrument_id="TWSE:2317",
            prediction_artifact_version="predictions-v2",
            decision_time=decision,
            available_at=decision - timedelta(minutes=1),
            fit_cutoff=decision - timedelta(days=1),
        ),
    )
    with pytest.raises(ValueError, match="mixed prediction"):
        PredictionTargetBuilder().build(
            rows,
            {item.key: item for item in fixture.instruments},
            strategy_version="strategy-v1",
            universe_version="universe-v1",
            policy=PredictionTargetPolicy("top-v1", TargetPolicyKind.TOP_K),
        )


def test_prediction_targets_execute_only_at_next_session_open() -> None:
    fixture = synthetic_backtest_fixture()
    predictions = _prediction_ledger(fixture)
    policy = PredictionTargetPolicy("top-v1", TargetPolicyKind.TOP_K, top_k=2)
    strategy = _strategy(
        fixture, predictions, policy, RebalancePolicy("daily-v1", RebalanceFrequency.DAILY)
    )
    result = EventDrivenBacktestEngine().run(fixture.config, fixture.sessions, strategy)
    fills = [event for event in result.events if event.kind.value == "fill.received"]
    assert fills
    assert all(
        event.occurred_at.date() > fixture.sessions[0].decision_time.date()
        for event in fills[:2]
    )
    assert all(event.payload["reference_source"] == "next_session_open" for event in fills)


def test_performance_returns_metrics_and_sample_guard() -> None:
    fixture = synthetic_backtest_fixture()
    result = EventDrivenBacktestEngine().run(
        fixture.config,
        fixture.sessions,
        _strategy(
            fixture,
            _prediction_ledger(fixture),
            PredictionTargetPolicy("top-v1", TargetPolicyKind.TOP_K, top_k=2),
            RebalancePolicy("daily-v1", RebalanceFrequency.DAILY),
        ),
    )
    report = PerformanceAnalyzer().analyze(result, PerformancePolicy())
    assert report.total_return == report.ending_equity / report.initial_equity - 1
    assert report.daily_mean_return is not None
    assert report.annualized_volatility is not None
    sparse = PerformanceAnalyzer().analyze(
        result, PerformancePolicy(minimum_return_observations=99)
    )
    assert sparse.sharpe_ratio is sparse.sortino_ratio is sparse.daily_mean_return is None
    with pytest.raises(ValueError, match="external cash"):
        PerformanceAnalyzer().analyze(
            result, PerformancePolicy(), external_cash_flows=((date(2024, 1, 2), Decimal("1")),)
        )


def test_drawdown_start_trough_recovery_and_ongoing() -> None:
    days = [date(2024, 1, day) for day in range(1, 6)]
    _, start, trough, recovery, duration, ongoing = _drawdowns(
        days, [Decimal(value) for value in (100, 120, 90, 100, 120)]
    )
    assert (start, trough, recovery, duration, ongoing) == (
        days[1], days[2], days[4], 3, False
    )
    _, _, _, recovery, _, ongoing = _drawdowns(
        days[:4], [Decimal(value) for value in (100, 120, 90, 100)]
    )
    assert recovery is None and ongoing is True


def test_benchmark_alignment_initial_boundary_and_pit_market_cap() -> None:
    returns = (
        (date(2024, 1, 2), Decimal("0.01")),
        (date(2024, 1, 3), Decimal("0.02")),
        (date(2024, 1, 4), Decimal("-0.01")),
    )
    benchmark = BenchmarkSeries(
        "TAIEX-fixture",
        "TAIEX",
        "benchmark-v1",
        Decimal("100"),
        tuple(
            (day, value)
            for (day, _), value in zip(
                returns,
                (Decimal("101"), Decimal("102"), Decimal("101")),
                strict=True,
            )
        ),
    )
    report = analyze_benchmark(returns, benchmark, 252)
    assert report.daily[0][2] == Decimal("0.01")
    with pytest.raises(ValueError, match="align exactly"):
        analyze_benchmark(returns[:-1], benchmark, 252)
    with pytest.raises(ValueError, match="PIT market cap"):
        BenchmarkSeries(
            "universe", "eligible_universe_market_cap", "benchmark-v1", Decimal("1"), (), False
        )


def test_attribution_reconciles_exactly_and_slippage_has_correct_sign() -> None:
    fixture = synthetic_backtest_fixture()
    result = EventDrivenBacktestEngine().run(
        fixture.config,
        fixture.sessions,
        _strategy(
            fixture,
            _prediction_ledger(fixture),
            PredictionTargetPolicy("top-v1", TargetPolicyKind.TOP_K, top_k=2),
            RebalancePolicy("daily-v1", RebalanceFrequency.DAILY),
        ),
    )
    performance = PerformanceAnalyzer().analyze(result, PerformancePolicy())
    report = attribute(result, performance)
    assert report.portfolio.residual == Decimal("0")
    assert all(row.residual == Decimal("0") for row in report.per_session)
    assert performance.slippage_cost >= 0
    fills = [event for event in result.events if event.kind.value == "fill.received"]
    for event in fills:
        assert "reference_price" in event.payload and "tick_policy_version" in event.payload

    with pytest.raises(RuntimeError, match="residual is non-zero"):
        attribute(result, replace(performance, dividend_income=Decimal("1")))


def test_attribution_fails_closed_for_unsupported_stock_split_economics() -> None:
    fixture = synthetic_backtest_fixture("attribution-actions")
    instrument = fixture.instruments[0]
    actions = (
        CashDividend(
            "dividend-v1",
            instrument,
            fixture.sessions[1].trade_date,
            fixture.sessions[1].trade_date,
            fixture.config.trading_calendar[3],
            Decimal("1"),
            fixture.sessions[0].decision_time,
            "dividend-source-v1",
        ),
        StockSplit(
            "split-v1",
            instrument,
            fixture.sessions[2].trade_date,
            Decimal("2"),
            fixture.sessions[0].decision_time,
            "split-source-v1",
        ),
    )
    result = EventDrivenBacktestEngine().run(
        fixture.config,
        fixture.sessions,
        EqualWeightFixtureStrategy(),
        actions,
    )
    performance = PerformanceAnalyzer().analyze(result, PerformancePolicy())
    assert performance.dividend_income > 0
    with pytest.raises(ValueError, match="corporate-action economic attribution is unsupported"):
        attribute(result, performance)


def test_performance_policy_ddof_is_validated_and_applied() -> None:
    fixture = synthetic_backtest_fixture("ddof")
    result = EventDrivenBacktestEngine().run(
        fixture.config, fixture.sessions, EqualWeightFixtureStrategy()
    )
    sample = PerformanceAnalyzer().analyze(result, PerformancePolicy(volatility_ddof=1))
    assert sample.policy.volatility_ddof == 1
    with pytest.raises(ValueError, match="ddof"):
        PerformancePolicy(volatility_ddof=0)
    with pytest.raises(ValueError, match="ddof"):
        PerformancePolicy(volatility_ddof=2)


def test_scenario_runner_saves_failures_and_does_not_select_a_winner() -> None:
    scenarios = tuple(
        ScenarioConfig(
            "grid-v1",
            Decimal(value),
            Decimal(".1"),
            Decimal("1"),
            Decimal("1"),
            "daily",
            1,
            1,
        )
        for value in (0, 5, 10)
    )
    calls: list[str] = []

    def evaluator(config: ScenarioConfig) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        calls.append(config.checksum)
        if config.slippage_bps == 5:
            raise ValueError("saved failure")
        return Decimal(".01"), Decimal("1"), config.slippage_bps, Decimal("100")

    outcomes = ScenarioRunner().run(scenarios, evaluator)
    assert len(outcomes) == len(calls) == 3
    assert {item.config.slippage_bps: item.status for item in outcomes} == {
        Decimal("0"): "completed",
        Decimal("5"): "failed",
        Decimal("10"): "completed",
    }
    assert len({item.checksum for item in outcomes}) == 3
    reversed_outcomes = ScenarioRunner().run(tuple(reversed(scenarios)), evaluator)
    assert {item.config.scenario_id: item.checksum for item in outcomes} == {
        item.config.scenario_id: item.checksum for item in reversed_outcomes
    }


def test_scenario_runner_records_non_arithmetic_failures() -> None:
    config = ScenarioConfig(
        "grid-v1", Decimal("0"), Decimal(".1"), Decimal("1"), Decimal("1"), "daily", 1, 1
    )

    def fail(_: ScenarioConfig) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        raise KeyError("untrusted detail must not escape")

    outcome = ScenarioRunner().run((config,), fail)[0]
    assert outcome.status == "failed"
    assert outcome.failure_reason_code == "KeyError"
    assert outcome.error == "scenario evaluation failed"


@pytest.fixture(scope="module")
def artifact_root(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    root = tmp_path_factory.mktemp("backtest-artifacts")
    artifact = build_demo_backtest_artifact(root)
    return root, artifact.manifest.artifact_version


def test_artifact_is_content_addressed_immutable_and_replayable(
    artifact_root: tuple[Path, str],
) -> None:
    root, version = artifact_root
    store = BacktestArtifactStore(root)
    payload = store.read(version)
    assert payload["manifest"]["artifact_version"] == version
    assert payload["manifest"]["prediction_artifact_version"] == "selected-oos-synthetic-v1"
    assert len(payload["sensitivity_results"]) == 540
    assert store.save(build_demo_backtest_artifact(root, dry_run=True)).name == "artifact.json"


def test_artifact_identity_ignores_creation_time_and_temp_root(
    artifact_root: tuple[Path, str], tmp_path: Path
) -> None:
    root, version = artifact_root
    original = build_demo_backtest_artifact(root, dry_run=True)
    changed_manifest = replace(
        original.manifest, created_time=original.manifest.created_time + timedelta(days=30)
    )
    changed = BacktestArtifact(
        changed_manifest,
        {**original.document, "manifest": asdict(changed_manifest)},
    )
    other_root = tmp_path / "other-root"
    saved = BacktestArtifactStore(other_root).save(changed)
    assert changed.manifest.artifact_version == version
    assert saved == other_root / "backtests" / version / "artifact.json"


def test_atomic_publish_is_idempotent_under_concurrent_writers(
    artifact_root: tuple[Path, str], tmp_path: Path
) -> None:
    root, version = artifact_root
    artifact = build_demo_backtest_artifact(root, dry_run=True)
    destination = tmp_path / "concurrent"

    def publish(_: int) -> Path:
        return BacktestArtifactStore(destination).save(artifact)

    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = tuple(pool.map(publish, range(8)))
    assert len(set(paths)) == 1
    assert (
        BacktestArtifactStore(destination).read(version)["manifest"]["artifact_version"]
        == version
    )
    assert not tuple((destination / "backtests").glob(".candidate-*"))


def test_artifact_reader_rejects_corruption_and_symlink_escape(
    artifact_root: tuple[Path, str], tmp_path: Path
) -> None:
    root, version = artifact_root
    corrupt_root = tmp_path / "corrupt"
    copied = corrupt_root / "backtests" / version
    shutil.copytree(root / "backtests" / version, copied)
    (copied / "artifact.sha256").write_text("0" * 64, encoding="ascii")
    with pytest.raises(RuntimeError, match="checksum"):
        BacktestArtifactStore(corrupt_root).read(version)

    symlink_root = tmp_path / "symlink"
    (symlink_root / "backtests").mkdir(parents=True)
    escaped = "a" * 64
    (symlink_root / "backtests" / escaped).symlink_to(root / "backtests" / version)
    with pytest.raises(RuntimeError, match="escapes|symlink"):
        BacktestArtifactStore(symlink_root).read(escaped)


def test_artifact_promotion_is_always_fail_closed(
    artifact_root: tuple[Path, str],
) -> None:
    root, version = artifact_root
    with pytest.raises(RuntimeError, match="validated promotion rejected"):
        BacktestArtifactStore(root).assert_validated_promotion_allowed(version)
    for forbidden in ("", "latest", "current"):
        with pytest.raises(ValueError, match="lowercase hex"):
            BacktestArtifactStore(root).read(forbidden)


def test_artifact_dashboard_is_read_only_sanitized_and_never_falls_back(
    artifact_root: tuple[Path, str],
) -> None:
    root, version = artifact_root
    query = BacktestArtifactQuery(BacktestArtifactStore(root), version)
    browser = TestClient(create_app(DashboardQueryService(backtest_query=query)))
    endpoints = (
        f"/api/dashboard/backtests/{version}",
        f"/api/dashboard/backtests/{version}/equity",
        f"/api/dashboard/backtests/{version}/attribution",
        f"/api/dashboard/backtests/{version}/orders",
    )
    for endpoint in endpoints:
        response = browser.get(endpoint)
        assert response.status_code == 200
        encoded = json.dumps(response.json()).lower()
        assert "/users/" not in encoded and "credential" not in encoded
    unknown = "f" * 64
    assert browser.get(f"/api/dashboard/backtests/{unknown}").status_code == 404
    assert browser.get(f"/backtests/{unknown}").status_code == 404
    assert browser.get("/api/dashboard/backtests/not-real").status_code == 422
    for unsafe in ("..", "%2e%2e", "%2Fetc", "%5Cwindows", "A" * 64, "a" * 65):
        assert browser.get(f"/api/dashboard/backtests/{unsafe}").status_code == 422
    assert browser.get("/api/dashboard/overview").status_code == 404
    assert browser.get("/health").json()["mode"] == "artifact-read-only"
    assert TestClient(create_app()).get("/api/dashboard/backtests").json() == {
        "mode": "demo",
        "artifact_version": None,
        "items": [],
    }


def test_artifact_dashboard_discloses_assumptions_warnings_and_no_recommendation(
    artifact_root: tuple[Path, str],
) -> None:
    root, version = artifact_root
    app = create_app(
        DashboardQueryService(
            backtest_query=BacktestArtifactQuery(BacktestArtifactStore(root), version)
        )
    )
    body = TestClient(app).get(f"/backtests/{version}").text
    for phrase in (
        "Execution assumptions",
        "Gross-before-costs",
        "Net-after-costs",
        "PIT completeness",
        "Dirty status",
        "Reconciliation status",
        "Daily returns and drawdown",
        "Benchmark comparison",
        "Orders, fills, and risk decisions",
        "Sensitivity scenarios",
        "none recommended",
        "SYNTHETIC / EXPLORATORY",
        "Notional-allocated attribution",
        "not security-level economic contribution",
    ):
        assert phrase in body


def test_backtest_cli_requires_pins_and_supports_offline_artifact_commands(
    artifact_root: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, version = artifact_root
    monkeypatch.setenv("ISLAND_QUANT__RESEARCH__ARTIFACT_ROOT", str(root))
    assert main(["--config", "config/default.yaml", "run-backtest", "--dry-run"]) == 2
    pinned: list[str] = []
    for name in (
        "prediction-artifact-version",
        "model-artifact-version",
        "dataset-version",
        "universe-version",
        "calendar-version",
        "corporate-action-version",
        "benchmark-version",
        "target-policy-version",
    ):
        pinned.extend((f"--{name}", f"{name}-v1"))
    assert main(["--config", "config/default.yaml", "run-backtest", "--dry-run", *pinned]) == 0
    assert main(
        ["--config", "config/default.yaml", "inspect-backtest", "--artifact-version", version]
    ) == 0
    assert main(
        [
            "--config",
            "config/default.yaml",
            "compare-backtests",
            "--left-version",
            version,
            "--right-version",
            version,
        ]
    ) == 0
    output = tmp_path / "report.md"
    assert main(
        [
            "--config",
            "config/default.yaml",
            "generate-backtest-report",
            "--artifact-version",
            version,
            "--output",
            str(output),
        ]
    ) == 0
    assert "not investment performance" in output.read_text(encoding="utf-8")
    assert main(
        [
            "--config",
            "config/default.yaml",
            "generate-backtest-report",
            "--artifact-version",
            version,
            "--output",
            str(output),
        ]
    ) == 2
    assert main(
        [
            "--config",
            "config/default.yaml",
            "generate-backtest-report",
            "--artifact-version",
            version,
            "--output",
            str(root / "report.md"),
        ]
    ) == 2
    with patch("uvicorn.run") as run:
        assert main(
            [
                "--config",
                "config/default.yaml",
                "dashboard",
                "--artifact-version",
                version,
            ]
        ) == 0
    assert run.call_args.kwargs["host"] == "127.0.0.1"
