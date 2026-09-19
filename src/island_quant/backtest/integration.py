"""Offline prediction-to-artifact integration fixture for engineering verification."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from island_quant.analytics.performance import (
    BenchmarkSeries,
    PerformanceAnalyzer,
    PerformancePolicy,
    analyze_benchmark,
    attribute,
)
from island_quant.analytics.scenarios import ScenarioRunner, default_scenario_grid
from island_quant.backtest.artifacts import BacktestArtifact, BacktestArtifactStore
from island_quant.backtest.engine import EventDrivenBacktestEngine
from island_quant.backtest.fixtures import SyntheticBacktestFixture, synthetic_backtest_fixture
from island_quant.backtest.policies import FillPolicy
from island_quant.backtest.predictions import (
    OOSPrediction,
    PredictionRole,
    SelectedPredictionLedger,
)
from island_quant.backtest.targets import (
    PinnedTargetStrategy,
    PredictionTargetBuilder,
    PredictionTargetPolicy,
    RebalanceFrequency,
    RebalancePolicy,
    TargetPolicyKind,
)


def build_demo_backtest_artifact(
    root: Path,
    *,
    dry_run: bool = False,
    created_time: datetime | None = None,
    seed: int = 42,
    artifact_config: dict[str, object] | None = None,
) -> BacktestArtifact:
    """Build a deterministic synthetic artifact; never represents observed performance."""
    fixture = synthetic_backtest_fixture("phase2-slice2-demo-v1")
    ledger = _prediction_ledger(fixture)
    policy = PredictionTargetPolicy(
        version="top-k-equal-weight-engineering-fixture-v1",
        kind=TargetPolicyKind.TOP_K,
        cash_buffer=Decimal("0.05"),
        maximum_position_weight=Decimal("0.50"),
        top_k=2,
    )
    rebalance = RebalancePolicy("daily-pinned-calendar-v1", RebalanceFrequency.DAILY)
    strategy = _strategy(fixture, ledger, policy, rebalance)
    result = EventDrivenBacktestEngine().run(fixture.config, fixture.sessions, strategy)
    performance = PerformanceAnalyzer().analyze(result, PerformancePolicy())
    benchmark_levels = tuple(
        (
            session.trade_date,
            sum((quote.close_price for quote in session.quotes), Decimal("0")),
        )
        for session in fixture.sessions
    )
    benchmark = analyze_benchmark(
        performance.daily_returns,
        BenchmarkSeries(
            "TAIEX-synthetic-fixture",
            "TAIEX",
            "benchmark-synthetic-v1",
            sum(
                (price for _, price in fixture.sessions[0].decision_prices), Decimal("0")
            ),
            benchmark_levels,
        ),
        performance.policy.annualization_factor,
    )
    attribution = attribute(result, performance)

    def evaluate_scenario(config: object) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        from island_quant.analytics.scenarios import ScenarioConfig

        if not isinstance(config, ScenarioConfig):
            raise TypeError("scenario config type mismatch")
        scenario_policy = replace(
            policy,
            version=f"{policy.version}:{config.checksum[:12]}",
            top_k=config.top_k,
        )
        frequency = RebalanceFrequency(config.rebalance_frequency)
        schedule = RebalancePolicy(
            f"scenario-{config.rebalance_frequency}-{config.rebalance_interval_sessions}",
            frequency,
            config.rebalance_interval_sessions,
        )
        scenario_strategy = _strategy(fixture, ledger, scenario_policy, schedule)
        fee = fixture.config.fee_policy
        scenario_fee = replace(
            fee,
            version=f"{fee.version}:x{config.fee_multiplier}",
            commission_rate=fee.commission_rate * config.fee_multiplier,
            minimum_commission=fee.minimum_commission * config.fee_multiplier,
            sell_transaction_tax_rate=(
                fee.sell_transaction_tax_rate * config.tax_multiplier
            ),
        )
        scenario_config = replace(
            fixture.config,
            run_id=f"scenario-{config.checksum[:16]}",
            fee_policy=scenario_fee,
            fill_policy=FillPolicy(
                version=f"scenario-fill-{config.checksum[:12]}",
                slippage_bps=config.slippage_bps,
                maximum_volume_participation=config.participation_cap,
            ),
        )
        scenario_result = EventDrivenBacktestEngine().run(
            scenario_config, fixture.sessions, scenario_strategy
        )
        report = PerformanceAnalyzer().analyze(scenario_result, PerformancePolicy())
        capacity = config.participation_cap * Decimal("100000")
        return report.total_return, report.turnover, report.slippage_cost, capacity

    scenarios = ScenarioRunner().run(default_scenario_grid(), evaluate_scenario)
    store = BacktestArtifactStore(root)
    artifact = store.build(
        result,
        performance,
        attribution,
        scenarios,
        target_policy_version=policy.version,
        prediction_artifact_version=ledger.version,
        model_artifact_version="linear-model-synthetic-v1",
        dataset_version="ohlcv-synthetic-v1",
        universe_version="universe-synthetic-v1",
        calendar_version="calendar-synthetic-v1",
        corporate_action_version="corporate-actions-none-v1",
        benchmark_version=benchmark.benchmark_version,
        benchmark=benchmark,
        seed=seed,
        created_time=created_time
        or datetime.fromisoformat("2024-01-08T00:00:00+08:00"),
        completeness_status="exploratory",
        synthetic_demo=True,
        config=(
            artifact_config
            if artifact_config is not None
            else {
                "target_policy": policy.version,
                "rebalance_policy": rebalance.version,
                "scenario_runner": ScenarioRunner.version,
            }
        ),
        input_checksums={
            "prediction_ledger": ledger.checksum,
            "targets": ",".join(item.checksum for item in strategy.artifacts),
        },
    )
    if not dry_run:
        store.save(artifact)
    return artifact


def _prediction_ledger(fixture: SyntheticBacktestFixture) -> SelectedPredictionLedger:
    records: list[OOSPrediction] = []
    eligible: set[tuple[str, date]] = set()
    boundaries: dict[str, tuple[date, date | None]] = {
        instrument.key: (fixture.sessions[0].trade_date - timedelta(days=365), None)
        for instrument in fixture.instruments
    }
    for session in fixture.sessions:
        for observation in session.predictions:
            eligible.add((observation.instrument.key, session.decision_time.date()))
            records.append(
                OOSPrediction.create(
                    strategy_run="phase2-slice2-demo-v1",
                    experiment_id="experiment-synthetic-v1",
                    fold_id=f"fold-{session.trade_date.isoformat()}",
                    model_artifact_version="linear-model-synthetic-v1",
                    feature_artifact_version="features-synthetic-v1",
                    label_version="next-open-return-synthetic-v1",
                    prediction_artifact_version="selected-oos-synthetic-v1",
                    instrument_id=observation.instrument.key,
                    decision_time=session.decision_time,
                    available_at=observation.available_at,
                    prediction=observation.value,
                    target_definition="next-session-open-to-close-return",
                    split_role=PredictionRole.TEST,
                    fit_cutoff=session.decision_time - timedelta(days=30),
                    completeness_status="exploratory",
                )
            )
    return SelectedPredictionLedger.build(
        "selected-oos-synthetic-v1",
        tuple(records),
        eligible_membership=eligible,
        listing_boundaries=boundaries,
    )


def _strategy(
    fixture: SyntheticBacktestFixture,
    ledger: SelectedPredictionLedger,
    policy: PredictionTargetPolicy,
    rebalance: RebalancePolicy,
) -> PinnedTargetStrategy:
    instruments = {instrument.key: instrument for instrument in fixture.instruments}
    artifacts = []
    targets = []
    builder = PredictionTargetBuilder()
    for session in fixture.sessions:
        artifact, positions = builder.build(
            ledger.at(session.decision_time),
            instruments,
            strategy_version="prediction-top-k-synthetic-v1",
            universe_version="universe-synthetic-v1",
            policy=policy,
        )
        artifacts.append(artifact)
        targets.append((session.decision_time, positions))
    return PinnedTargetStrategy(
        "prediction-top-k-synthetic-v1",
        "prediction-top-k-synthetic-v1",
        tuple(artifacts),
        tuple(targets),
        rebalance,
        tuple(session.trade_date for session in fixture.sessions),
        fixture.instruments,
    )
