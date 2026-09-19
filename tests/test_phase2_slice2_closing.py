from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, time
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
    analyze_benchmark,
    attribute,
)
from island_quant.analytics.scenarios import ScenarioConfig, ScenarioRunner
from island_quant.backtest.artifacts import (
    BacktestArtifact,
    BacktestArtifactStore,
    validate_artifact_version,
)
from island_quant.backtest.engine import EventDrivenBacktestEngine
from island_quant.backtest.fixtures import synthetic_backtest_fixture
from island_quant.backtest.integration import (
    _prediction_ledger,
    _strategy,
    build_demo_backtest_artifact,
)
from island_quant.backtest.policies import FillPolicy
from island_quant.backtest.targets import (
    CoverageFailureAction,
    MissingPredictionAction,
    PinnedTargetStrategy,
    PredictionTargetBuilder,
    PredictionTargetPolicy,
    RebalanceFrequency,
    RebalancePolicy,
    TargetPolicyKind,
)
from island_quant.cli import _escape_markdown, main
from island_quant.dashboard.app import create_app
from island_quant.dashboard.backtests import BacktestArtifactQuery
from island_quant.dashboard.service import DashboardQueryService
from island_quant.portfolio.accounting import CashDividend, StockSplit
from island_quant.strategies.fixtures import (
    BuyAndHoldFixtureStrategy,
    EqualWeightFixtureStrategy,
)

TAIPEI = ZoneInfo("Asia/Taipei")


@pytest.fixture(scope="module")
def saved_artifact(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, BacktestArtifact]:
    root = tmp_path_factory.mktemp("closing-artifact")
    artifact = build_demo_backtest_artifact(root)
    return root, artifact


@pytest.mark.parametrize(
    "version",
    (
        "",
        "..",
        "../secret",
        "a/b",
        r"a\b",
        "/absolute",
        "A" * 64,
        "a" * 63,
        "a" * 65,
        "%2e%2e",
        "%2fetc%2fpasswd",
    ),
)
def test_artifact_versions_are_opaque_canonical_digests(version: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="lowercase hex"):
        validate_artifact_version(version)
    with pytest.raises(ValueError, match="lowercase hex"):
        BacktestArtifactStore(tmp_path).read(version)
    with pytest.raises(ValueError, match="lowercase hex"):
        BacktestArtifactQuery(BacktestArtifactStore(tmp_path), version)


def test_cli_rejects_traversal_without_echoing_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ISLAND_QUANT__RESEARCH__ARTIFACT_ROOT", str(tmp_path))
    assert main(
        [
            "--config",
            "config/default.yaml",
            "inspect-backtest",
            "--artifact-version",
            "../private",
        ]
    ) == 2
    error = capsys.readouterr().err
    assert "invalid artifact version" in error
    assert "private" not in error and str(tmp_path) not in error


def test_api_invalid_encoded_traversal_is_422_and_unknown_digest_is_404(
    saved_artifact: tuple[Path, BacktestArtifact],
) -> None:
    root, artifact = saved_artifact
    query = BacktestArtifactQuery(BacktestArtifactStore(root), artifact.manifest.artifact_version)
    browser = TestClient(create_app(DashboardQueryService(backtest_query=query)))
    for path in (
        "/api/dashboard/backtests/not-a-digest",
        "/api/dashboard/backtests/%2e%2e",
        "/api/dashboard/backtests/%2Fetc%2Fpasswd",
        "/api/dashboard/backtests/" + "A" * 64,
    ):
        assert browser.get(path).status_code == 422
    assert browser.get("/api/dashboard/backtests/" + "f" * 64).status_code == 404


def test_symlink_escape_and_partial_candidate_are_never_read(
    tmp_path: Path, saved_artifact: tuple[Path, BacktestArtifact]
) -> None:
    _, artifact = saved_artifact
    store = BacktestArtifactStore(tmp_path)
    base = tmp_path / "backtests"
    base.mkdir()
    interrupted = base / ".candidate-interrupted"
    interrupted.mkdir()
    (interrupted / "artifact.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        store.read("e" * 64)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = base / artifact.manifest.artifact_version
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="escapes"):
        store.read(artifact.manifest.artifact_version)


def test_content_identity_excludes_clock_root_and_map_order(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = build_demo_backtest_artifact(
        first_root,
        dry_run=True,
        created_time=datetime(2024, 1, 8, tzinfo=TAIPEI),
        artifact_config={"alpha": 1, "beta": 2},
    )
    second = build_demo_backtest_artifact(
        second_root,
        dry_run=True,
        created_time=datetime(2026, 9, 19, tzinfo=TAIPEI),
        artifact_config={"beta": 2, "alpha": 1},
    )
    fields = (
        "artifact_version",
        "report_checksum",
        "event_journal_checksum",
        "ledger_checksum",
        "snapshot_checksum",
    )
    assert all(
        getattr(first.manifest, field) == getattr(second.manifest, field)
        for field in fields
    )
    idempotent_store = BacktestArtifactStore(tmp_path / "idempotent-clock")
    assert idempotent_store.save(first) == idempotent_store.save(second)
    changed = build_demo_backtest_artifact(
        tmp_path / "changed", dry_run=True, seed=43, artifact_config={"alpha": 1, "beta": 2}
    )
    assert changed.manifest.artifact_version != first.manifest.artifact_version


def test_atomic_publish_is_concurrent_idempotent_and_complete(
    tmp_path: Path, saved_artifact: tuple[Path, BacktestArtifact]
) -> None:
    _, artifact = saved_artifact
    store = BacktestArtifactStore(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = tuple(pool.map(store.save, (artifact,) * 4))
    assert len(set(paths)) == 1
    assert store.read(artifact.manifest.artifact_version)["manifest"][
        "artifact_version"
    ] == artifact.manifest.artifact_version
    assert not list((tmp_path / "backtests").glob(".candidate-*"))


def test_corrupted_checksum_incomplete_manifest_and_identity_collision_fail_closed(
    tmp_path: Path, saved_artifact: tuple[Path, BacktestArtifact]
) -> None:
    _, artifact = saved_artifact
    store = BacktestArtifactStore(tmp_path)
    path = store.save(artifact)
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(RuntimeError, match="file checksum"):
        store.read(artifact.manifest.artifact_version)

    other = BacktestArtifactStore(tmp_path / "incomplete")
    other_path = other.save(artifact)
    payload = json.loads(other_path.read_text(encoding="utf-8"))
    del payload["manifest"]["scenario_count"]
    content = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    other_path.write_bytes(content)
    other_path.with_name("artifact.sha256").write_text(
        hashlib.sha256(content).hexdigest(), encoding="ascii"
    )
    with pytest.raises(RuntimeError, match="incomplete"):
        other.read(artifact.manifest.artifact_version)

    with pytest.raises(RuntimeError, match="checksum"):
        store.save(
            BacktestArtifact(
                artifact.manifest,
                {**artifact.document, "warnings": ["different semantic content"]},
            )
        )


def test_scenario_grid_manifest_limits_duplicates_failures_and_order_independence(
    saved_artifact: tuple[Path, BacktestArtifact],
) -> None:
    _, artifact = saved_artifact
    manifest = artifact.manifest
    assert manifest.scenario_count == 540
    assert len(manifest.scenario_config_checksums) == 540
    assert len(set(manifest.scenario_config_checksums)) == 540
    scenario = ScenarioConfig(
        "closing-v1", Decimal("5"), Decimal(".1"), Decimal("1"), Decimal("1"), "daily", 1, 2
    )
    with pytest.raises(ValueError, match="duplicate"):
        ScenarioRunner().run((scenario, scenario), lambda _: (Decimal("0"),) * 4)
    with pytest.raises(ValueError, match="maximum"):
        ScenarioRunner(maximum_scenarios=1).run(
            (scenario, replace(scenario, top_k=3)), lambda _: (Decimal("0"),) * 4
        )

    def evaluate(item: ScenarioConfig) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        if item.top_k == 3:
            raise ValueError("internal detail must not leak")
        value = Decimal(item.top_k)
        return value, value, value, value

    ordered = ScenarioRunner().run((scenario, replace(scenario, top_k=3)), evaluate)
    reversed_rows = ScenarioRunner().run(
        (replace(scenario, top_k=3), scenario), evaluate
    )
    assert {item.config.scenario_id: item.checksum for item in ordered} == {
        item.config.scenario_id: item.checksum for item in reversed_rows
    }
    failed = next(item for item in ordered if item.status == "failed")
    assert failed.failure_reason_code == "ValueError"
    assert failed.error == "scenario evaluation failed"


def test_missing_prediction_coverage_fails_closed_or_skips_and_single_gap_is_typed() -> None:
    fixture = synthetic_backtest_fixture()
    ledger = _prediction_ledger(fixture)
    policy = PredictionTargetPolicy("coverage-top-v1", TargetPolicyKind.TOP_K, top_k=2)
    base = _strategy(
        fixture,
        ledger,
        policy,
        RebalancePolicy("coverage-default-v1", RebalanceFrequency.DAILY),
    )
    no_date = replace(base, targets_by_decision=base.targets_by_decision[1:])
    with pytest.raises(ValueError, match="coverage is zero"):
        no_date.targets(fixture.sessions[0])
    skipped = replace(
        no_date,
        rebalance_policy=RebalancePolicy(
            "coverage-skip-v1",
            RebalanceFrequency.DAILY,
            coverage_failure_action=CoverageFailureAction.SKIP_REBALANCE,
        ),
    )
    assert skipped.targets(fixture.sessions[0]) == ()

    first_records = ledger.at(fixture.sessions[0].decision_time)[:-1]
    target_artifact, positions = PredictionTargetBuilder().build(
        first_records,
        {item.key: item for item in fixture.instruments},
        strategy_version=base.strategy_id,
        universe_version="universe-synthetic-v1",
        policy=policy,
    )
    partial = PinnedTargetStrategy(
        base.strategy_id,
        base.version,
        (target_artifact,),
        ((fixture.sessions[0].decision_time, positions),),
        RebalancePolicy(
            "coverage-partial-v1",
            RebalanceFrequency.DAILY,
            missing_prediction_action=MissingPredictionAction.LIQUIDATE_TO_CASH,
            minimum_prediction_coverage=Decimal("0.50"),
        ),
        tuple(session.trade_date for session in fixture.sessions),
        fixture.instruments,
    )
    targets = partial.targets(fixture.sessions[0])
    forced = [item for item in targets if item.reason == "data_quality:missing_artifact_row"]
    assert len(forced) == 1 and forced[0].target_weight == 0


def test_performance_metadata_and_edge_cases() -> None:
    fixture = synthetic_backtest_fixture("performance-closing")
    result = EventDrivenBacktestEngine().run(
        fixture.config,
        fixture.sessions,
        BuyAndHoldFixtureStrategy(fixture.instruments[0], fixture.sessions[0].trade_date),
    )
    report = PerformanceAnalyzer().analyze(result, PerformancePolicy())
    assert report.policy.return_frequency == "trading_session"
    assert report.policy.volatility_ddof == 1
    assert report.policy.cagr_elapsed_time_convention == "actual_calendar_days_365.25"
    assert report.policy.exposure_sampling_time == "session_close"
    assert report.policy.cash_weight_sampling_time.startswith("session_close")
    assert report.cagr is not None

    unchanged = tuple(
        replace(snapshot, net_asset_value=fixture.config.initial_cash)
        for snapshot in result.snapshots
    )
    flat = replace(result, snapshots=unchanged, final_snapshot=unchanged[-1])
    flat_report = PerformanceAnalyzer().analyze(flat, PerformancePolicy())
    assert flat_report.annualized_volatility == 0
    assert flat_report.sharpe_ratio is None and flat_report.sortino_ratio is None
    short = PerformanceAnalyzer().analyze(
        replace(result, snapshots=result.snapshots[:2], final_snapshot=result.snapshots[1]),
        PerformancePolicy(),
    )
    assert short.sharpe_ratio is None and short.annualized_volatility is None
    zero = replace(result.snapshots[1], net_asset_value=Decimal("0"))
    with pytest.raises(ValueError, match="every session"):
        PerformanceAnalyzer().analyze(
            replace(
                result,
                snapshots=(result.snapshots[0], zero, result.snapshots[2]),
                final_snapshot=result.snapshots[2],
            ),
            PerformancePolicy(),
        )


def test_benchmark_constant_has_no_beta_or_alpha_and_drawdown_can_remain_open() -> None:
    returns = tuple(
        (date(2024, 1, day), value)
        for day, value in ((2, Decimal("-.01")), (5, Decimal(".01")), (9, Decimal("-.02")))
    )
    benchmark = BenchmarkSeries(
        "constant",
        "TAIEX",
        "constant-v1",
        Decimal("100"),
        tuple((day, Decimal("100")) for day, _ in returns),
    )
    report = analyze_benchmark(returns, benchmark, 252)
    assert report.beta is None and report.alpha_annual is None

    fixture = synthetic_backtest_fixture("drawdown-closing")
    result = EventDrivenBacktestEngine().run(
        fixture.config,
        fixture.sessions,
        BuyAndHoldFixtureStrategy(fixture.instruments[0], fixture.sessions[0].trade_date),
    )
    snapshots = tuple(
        replace(snapshot, net_asset_value=value)
        for snapshot, value in zip(
            result.snapshots,
            (Decimal("110000"), Decimal("105000"), Decimal("90000")),
            strict=True,
        )
    )
    drawdown = PerformanceAnalyzer().analyze(
        replace(result, snapshots=snapshots, final_snapshot=snapshots[-1]),
        PerformancePolicy(),
    )
    assert drawdown.drawdown_ongoing is True and drawdown.maximum_drawdown_recovery is None


def test_irregular_leap_year_calendar_cagr_and_negative_excess_return() -> None:
    fixture = synthetic_backtest_fixture("irregular-performance")
    result = EventDrivenBacktestEngine().run(
        fixture.config,
        fixture.sessions,
        BuyAndHoldFixtureStrategy(fixture.instruments[0], fixture.sessions[0].trade_date),
    )
    dates = (date(2023, 12, 29), date(2024, 2, 29), date(2025, 1, 2))
    navs = (Decimal("100000"), Decimal("99000"), Decimal("97000"))
    snapshots = tuple(
        replace(
            snapshot,
            as_of=datetime.combine(day, time(13, 30), tzinfo=TAIPEI),
            net_asset_value=nav,
        )
        for snapshot, day, nav in zip(result.snapshots, dates, navs, strict=True)
    )
    irregular = replace(result, snapshots=snapshots, final_snapshot=snapshots[-1])
    report = PerformanceAnalyzer().analyze(
        irregular,
        PerformancePolicy(risk_free_rate_annual=Decimal(".02")),
    )
    elapsed = (dates[-1] - dates[0]).days
    expected = float(
        (navs[-1] / fixture.config.initial_cash)
        ** (Decimal("365.25") / elapsed)
        - Decimal("1")
    )
    assert report.cagr == pytest.approx(expected)
    assert report.sharpe_ratio is not None and report.sharpe_ratio < 0


def test_attribution_boundaries_reconcile_and_unsupported_split_fails_closed() -> None:
    fixture = synthetic_backtest_fixture("attribution-closing")
    config = replace(
        fixture.config,
        risk_policy=replace(fixture.config.risk_policy, maximum_position_weight=Decimal("1")),
        fill_policy=FillPolicy(
            slippage_bps=Decimal("10"), maximum_volume_participation=Decimal("1")
        ),
    )
    result = EventDrivenBacktestEngine().run(
        config,
        fixture.sessions,
        EqualWeightFixtureStrategy(),
    )
    performance = PerformanceAnalyzer().analyze(result, PerformancePolicy())
    attribution = attribute(result, performance)
    assert attribution.portfolio.residual == 0
    assert sum((row.total for row in attribution.per_session), Decimal("0")) == (
        attribution.portfolio.total
    )
    assert attribution.allocation_warning.startswith("Executed-notional allocation")
    assert sum(
        (row.total for row in attribution.notional_allocated_attribution), Decimal("0")
    ) == attribution.portfolio.total
    assert result.final_snapshot.position_quantities
    assert result.final_snapshot.settlement_receivables >= 0
    assert result.final_snapshot.settlement_payables >= 0
    sides = {
        event.payload["fill"]["side"]
        for event in result.events
        if event.kind.value == "fill.received" and event.payload.get("fill")
    }
    assert sides == {"buy", "sell"}
    assert performance.slippage_cost > 0
    assert any(snapshot.settlement_payables > 0 for snapshot in result.snapshots)

    partial_result = EventDrivenBacktestEngine().run(
        replace(
            config,
            run_id="attribution-partial",
            fill_policy=replace(config.fill_policy, maximum_volume_participation=Decimal(".001")),
        ),
        fixture.sessions,
        EqualWeightFixtureStrategy(),
    )
    partial_performance = PerformanceAnalyzer().analyze(partial_result, PerformancePolicy())
    assert partial_performance.partial_fills > 0
    assert attribute(partial_result, partial_performance).portfolio.residual == 0

    dividend = CashDividend(
        "closing-dividend",
        fixture.instruments[0],
        fixture.sessions[1].trade_date,
        fixture.sessions[1].trade_date,
        fixture.config.trading_calendar[3],
        Decimal("1"),
        fixture.sessions[0].close_time,
        "dividend-v1",
    )
    dividend_result = EventDrivenBacktestEngine().run(
        replace(config, run_id="attribution-dividend"),
        fixture.sessions,
        EqualWeightFixtureStrategy(),
        (dividend,),
    )
    dividend_report = PerformanceAnalyzer().analyze(dividend_result, PerformancePolicy())
    assert attribute(dividend_result, dividend_report).portfolio.dividend_pnl > 0

    split = StockSplit(
        "closing-split",
        fixture.instruments[0],
        fixture.sessions[1].trade_date,
        Decimal("2"),
        fixture.sessions[0].close_time,
        "split-v1",
    )
    split_result = EventDrivenBacktestEngine().run(
        replace(config, run_id="attribution-split"),
        fixture.sessions,
        EqualWeightFixtureStrategy(),
        (split,),
    )
    with pytest.raises(ValueError, match="corporate-action economic attribution"):
        attribute(split_result, PerformanceAnalyzer().analyze(split_result, PerformancePolicy()))


def test_attribution_cash_only_no_trade_price_move_and_dividend_boundaries() -> None:
    fixture = synthetic_backtest_fixture("attribution-boundaries")
    config = replace(
        fixture.config,
        risk_policy=replace(fixture.config.risk_policy, maximum_position_weight=Decimal("1")),
        fill_policy=FillPolicy(
            slippage_bps=Decimal("0"), maximum_volume_participation=Decimal("1")
        ),
    )

    class CashStrategy:
        strategy_id = "cash-only"
        version = "cash-only-v1"

        def targets(self, session: object) -> tuple[object, ...]:
            return ()

    cash = EventDrivenBacktestEngine().run(
        replace(config, run_id="cash-only"),
        fixture.sessions,
        CashStrategy(),  # type: ignore[arg-type]
    )
    cash_attribution = attribute(cash, PerformanceAnalyzer().analyze(cash, PerformancePolicy()))
    assert cash_attribution.portfolio.total == 0
    assert all(row.total == 0 for row in cash_attribution.per_session)

    held = EventDrivenBacktestEngine().run(
        replace(config, run_id="held-price-move"),
        fixture.sessions,
        BuyAndHoldFixtureStrategy(fixture.instruments[0], fixture.sessions[0].trade_date),
    )
    held_attribution = attribute(held, PerformanceAnalyzer().analyze(held, PerformancePolicy()))
    fills_by_day = {
        event.occurred_at.date()
        for event in held.events
        if event.kind.value == "fill.received" and event.payload.get("fill")
    }
    no_trade_price_rows = [
        row
        for row in held_attribution.per_session
        if date.fromisoformat(row.key) not in fills_by_day and row.gross_price_movement != 0
    ]
    assert no_trade_price_rows

    fixed_price = fixture.sessions[0].quotes[0].open_price
    constant_sessions = tuple(
        replace(
            session,
            quotes=tuple(
                replace(quote, open_price=fixed_price, close_price=fixed_price)
                for quote in session.quotes
            ),
            decision_prices=tuple(
                (instrument.key, fixed_price) for instrument in fixture.instruments
            ),
        )
        for session in fixture.sessions
    )
    dividend = CashDividend(
        "constant-dividend",
        fixture.instruments[0],
        constant_sessions[1].trade_date,
        constant_sessions[1].trade_date,
        fixture.config.trading_calendar[3],
        Decimal("1"),
        constant_sessions[0].close_time,
        "constant-dividend-v1",
    )
    constant = EventDrivenBacktestEngine().run(
        replace(config, run_id="constant-price-dividend"),
        constant_sessions,
        BuyAndHoldFixtureStrategy(fixture.instruments[0], constant_sessions[0].trade_date),
        (dividend,),
    )
    constant_attr = attribute(
        constant, PerformanceAnalyzer().analyze(constant, PerformancePolicy())
    )
    assert constant_attr.portfolio.dividend_pnl > 0
    assert constant_attr.portfolio.residual == 0


def test_data_quality_liquidation_turnover_and_costs_are_separate() -> None:
    fixture = synthetic_backtest_fixture("coverage-turnover")
    ledger = _prediction_ledger(fixture)
    policy = PredictionTargetPolicy(
        "coverage-turnover-top-v1",
        TargetPolicyKind.TOP_K,
        cash_buffer=Decimal(".05"),
        maximum_position_weight=Decimal(".5"),
        top_k=2,
    )
    builder = PredictionTargetBuilder()
    instruments = {item.key: item for item in fixture.instruments}
    artifacts = []
    targets_by_decision = []
    missing_key = fixture.instruments[2].key
    for index, session in enumerate(fixture.sessions):
        records = ledger.at(session.decision_time)
        if index == 1:
            records = tuple(item for item in records if item.instrument_id != missing_key)
        artifact, positions = builder.build(
            records,
            instruments,
            strategy_version="coverage-turnover-strategy-v1",
            universe_version="universe-synthetic-v1",
            policy=policy,
        )
        artifacts.append(artifact)
        targets_by_decision.append((session.decision_time, positions))
    strategy = PinnedTargetStrategy(
        "coverage-turnover-strategy-v1",
        "coverage-turnover-strategy-v1",
        tuple(artifacts),
        tuple(targets_by_decision),
        RebalancePolicy(
            "coverage-turnover-rebalance-v1",
            RebalanceFrequency.DAILY,
            minimum_prediction_coverage=Decimal(".5"),
        ),
        tuple(session.trade_date for session in fixture.sessions),
        fixture.instruments,
    )
    result = EventDrivenBacktestEngine().run(fixture.config, fixture.sessions, strategy)
    report = PerformanceAnalyzer().analyze(result, PerformancePolicy())
    assert report.forced_liquidation_count > 0
    assert report.data_quality_driven_turnover > 0
    assert report.strategy_driven_turnover + report.data_quality_driven_turnover == report.turnover
    assert report.forced_liquidation_costs > 0


def test_dashboard_modes_status_text_and_orders_pagination(
    saved_artifact: tuple[Path, BacktestArtifact],
) -> None:
    root, artifact = saved_artifact
    version = artifact.manifest.artifact_version
    demo = TestClient(create_app())
    assert demo.get("/api/dashboard/backtests").json()["mode"] == "demo"
    artifact_client = TestClient(
        create_app(
            DashboardQueryService(
                backtest_query=BacktestArtifactQuery(BacktestArtifactStore(root), version)
            )
        )
    )
    listing = artifact_client.get("/api/dashboard/backtests").json()
    assert listing["mode"] == "artifact" and listing["artifact_version"] == version
    assert artifact_client.get(
        f"/api/dashboard/backtests/{version}/orders?page=1&page_size=1"
    ).json()["page_size"] == 1
    assert artifact_client.get(
        f"/api/dashboard/backtests/{version}/orders?page_size=101"
    ).status_code == 422
    orders = artifact_client.get(f"/api/dashboard/backtests/{version}/orders").json()
    encoded = json.dumps(orders)
    assert "causation_id" not in encoded and '"inputs"' not in encoded
    detail = artifact_client.get(f"/backtests/{version}").text
    for text in ("ARTIFACT", "Synthetic", "Dirty status", "PIT completeness", "residual"):
        assert text in detail


def test_dashboard_cli_modes_reject_ambiguity_and_integrity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ISLAND_QUANT__RESEARCH__ARTIFACT_ROOT", str(tmp_path))
    assert main(["--config", "config/default.yaml", "dashboard"]) == 2
    assert main(
        [
            "--config",
            "config/default.yaml",
            "dashboard",
            "--demo",
            "--artifact-version",
            "a" * 64,
        ]
    ) == 2
    with patch("uvicorn.run") as run:
        assert main(
            [
                "--config",
                "config/default.yaml",
                "dashboard",
                "--artifact-version",
                "a" * 64,
            ]
        ) == 2
    run.assert_not_called()


def test_report_refuses_overwrite_and_artifact_root_and_escapes_markdown(
    saved_artifact: tuple[Path, BacktestArtifact],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, artifact = saved_artifact
    version = artifact.manifest.artifact_version
    monkeypatch.setenv("ISLAND_QUANT__RESEARCH__ARTIFACT_ROOT", str(root))
    output = tmp_path / "report.md"
    arguments = [
        "--config",
        "config/default.yaml",
        "generate-backtest-report",
        "--artifact-version",
        version,
        "--output",
        str(output),
    ]
    assert main(arguments) == 0
    assert main(arguments) == 2
    assert main([*arguments, "--force"]) == 0
    report = output.read_text(encoding="utf-8")
    for label in (
        "Classification",
        "PIT status",
        "Synthetic",
        "Dirty",
        "Fee/tax policy",
        "Execution model",
        "Reconciliation residual",
        version,
    ):
        assert label in report
    forbidden_output = root / "unsafe-report.md"
    assert main([*arguments[:-1], str(forbidden_output)]) == 2
    assert _escape_markdown("external_[name]*") == r"external\_\[name\]\*"


def test_demo_dry_run_reports_work_without_creating_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ISLAND_QUANT__RESEARCH__ARTIFACT_ROOT", str(tmp_path))
    assert main(
        ["--config", "config/default.yaml", "run-backtest", "--demo", "--dry-run"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scenario_count"] == 540
    assert payload["estimated_engine_runs"] == 541
    assert payload["artifact_namespace"] == "demo"
    assert not (tmp_path / "demo").exists()
