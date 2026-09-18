"""Deterministic synthetic dashboard fixture; never reads production artifacts."""

from __future__ import annotations

import math
import platform
from datetime import UTC, date, datetime, timedelta

from island_quant.dashboard.models import (
    ActivityEvent,
    BacktestView,
    ChartPoint,
    ChartSeries,
    DashboardContext,
    DataIssue,
    ExperimentDetail,
    ExperimentSummary,
    FactorDetail,
    FactorSummary,
    FoldMetric,
    HealthCheck,
    PipelineStage,
    SummaryCard,
    SystemView,
)

DEMO_SEED = 20240918
DATASET_VERSION = "demo-prices-2024q1-v1"
FEATURE_VERSION = "demo-baseline-14-v1"
LABEL_VERSION = "demo-forward-o2o-v1"
AVAILABILITY_VERSION = "tw-daily-demo-v1"
WARNING = "Incomplete PIT reference data — metrics cannot be used as production evidence."
SYNTHETIC_WARNING = (
    "Synthetic engineering fixture — perfect metrics are expected and have no statistical "
    "meaning."
)


def _series(name: str, color: str, values: list[float], prefix: str = "D") -> ChartSeries:
    return ChartSeries(
        name=name,
        color=color,
        points=tuple(
            ChartPoint(label=f"{prefix}{index + 1}", value=round(value, 6))
            for index, value in enumerate(values)
        ),
    )


class DemoDashboardFixture:
    """In-memory, fixed-seed view-model source with no filesystem/network access."""

    seed = DEMO_SEED
    session_count = 90
    instrument_count = 15

    def __init__(self) -> None:
        self.context = DashboardContext(
            dataset_version=DATASET_VERSION,
            last_artifact_update=datetime(2024, 5, 10, 18, 35, tzinfo=UTC),
            universes=("TWSE + TPEx demo", "TWSE demo", "TPEx demo"),
            selected_universe="TWSE + TPEx demo",
            selected_date_range="2024-01-02 — 2024-05-10",
        )
        self.factors = self._factors()
        self.factor_details = {item.factor_id: self._factor_detail(item) for item in self.factors}
        self.experiments = self._experiments()
        self.experiment_details = {
            item.experiment_id: self._experiment_detail(item, index)
            for index, item in enumerate(self.experiments)
        }
        self.issues = self._issues()

    def overview_cards(self) -> tuple[SummaryCard, ...]:
        return (
            SummaryCard(label="Dataset version", value=DATASET_VERSION),
            SummaryCard(label="Universe coverage", value="86.7%", status="warning"),
            SummaryCard(label="Valid instruments", value="13", status="positive"),
            SummaryCard(label="Invalid / excluded", value="2", status="warning"),
            SummaryCard(label="Feature count", value="14"),
            SummaryCard(label="Candidate models", value="3"),
            SummaryCard(label="PIT completeness", value="Incomplete", status="negative"),
            SummaryCard(label="Last successful stage", value="ML experiments"),
        )

    def pipeline(self) -> tuple[PipelineStage, ...]:
        states = (
            ("Data ingestion", "Complete", "Deterministic demo snapshot loaded"),
            ("Validation", "Complete", "Schema and price checks completed"),
            ("Universe", "Exploratory", "PIT reference history remains incomplete"),
            ("Features", "Complete", "14 baseline factors materialized"),
            ("Labels", "Complete", "Gross forward-return labels built"),
            ("Factor evaluation", "Exploratory", "Fixture metrics only"),
            ("ML experiments", "Exploratory", "Candidate models only"),
            ("Backtest", "Not started", "Phase 2 intentionally paused"),
            ("Paper trading", "Blocked", "Requires backtest and safety gates"),
            ("Live trading", "Blocked", "Disabled; no broker configured"),
        )
        return tuple(
            PipelineStage(order=index, name=name, status=status, detail=detail)  # type: ignore[arg-type]
            for index, (name, status, detail) in enumerate(states, 1)
        )

    def activities(self) -> tuple[ActivityEvent, ...]:
        base = datetime(2024, 5, 10, 10, tzinfo=UTC)
        return (
            ActivityEvent(created_at=base, kind="Dataset", message="Demo candidate created"),
            ActivityEvent(
                created_at=base + timedelta(hours=1),
                kind="Features",
                message="Synthetic baseline-14 feature set materialized",
            ),
            ActivityEvent(
                created_at=base + timedelta(hours=2),
                kind="Experiment",
                message="Demo walk-forward experiment completed",
            ),
            ActivityEvent(
                created_at=base + timedelta(hours=3),
                kind="Registry",
                message="Promotion rejected: incomplete PIT reference data",
            ),
        )

    def coverage_chart(self) -> ChartSeries:
        return _series(
            "Eligible coverage",
            "#5fbf91",
            [78 + index * 0.11 + math.sin(index / 7) * 2 for index in range(90)],
        )

    def validity_charts(self) -> tuple[ChartSeries, ...]:
        return (
            _series("Valid", "#65b88c", [91 + math.sin(i / 8) * 3 for i in range(30)]),
            _series("Missing", "#d9a441", [9 - math.sin(i / 8) * 3 for i in range(30)]),
        )

    def sample_chart(self) -> ChartSeries:
        return _series("Daily samples", "#74a7d8", [11 + (index % 5) for index in range(45)])

    def quality_chart(self) -> ChartSeries:
        labels = ("Unknown market", "Listing date", "Corp action", "Suspension", "Missing obs")
        return ChartSeries(
            name="Issues",
            color="#d9a441",
            points=tuple(
                ChartPoint(label=label, value=value)
                for label, value in zip(labels, (1, 3, 2, 4, 5), strict=True)
            ),
        )

    def _issues(self) -> tuple[DataIssue, ...]:
        categories = (
            ("unknown_market", "warning", "Historical market cannot be confirmed"),
            ("provisional_listing_date", "warning", "Listing date derived from first price"),
            ("unsupported_corporate_action", "warning", "Fixture action needs manual review"),
            ("missing_suspension_history", "error", "Suspension reference is incomplete"),
            ("missing_price_limit_tradability", "warning", "Limit-lock state unavailable"),
            ("provider_missing_observation", "info", "Provider omitted expected session"),
            ("invalid_ohlc", "error", "High is below close in synthetic bad row"),
            ("duplicate_primary_key", "error", "Duplicate instrument/session key"),
        )
        return tuple(
            DataIssue(
                issue_id=f"demo-issue-{index:03d}",
                instrument_id=f"D{1000 + index % 15}",
                event_date=date(2024, 1, 2) + timedelta(days=index * 6),
                category=category,
                severity=severity,  # type: ignore[arg-type]
                reason=reason,
                source="synthetic_fixture",
                resolution_status="fixture-only" if index > 4 else "open",
            )
            for index, (category, severity, reason) in enumerate(categories, 1)
        )

    def _factors(self) -> tuple[FactorSummary, ...]:
        definitions = (
            ("momentum_5", "Momentum 5", "Momentum", 5, "long_high"),
            ("momentum_20", "Momentum 20", "Momentum", 20, "long_high"),
            ("reversal_5", "Reversal 5", "Reversal", 5, "long_low"),
            ("intraday_return", "Intraday return", "Price", 1, "long_high"),
            ("overnight_gap", "Overnight gap", "Price", 2, "long_high"),
            ("realized_volatility_20", "Realized volatility", "Risk", 20, "long_low"),
            ("downside_volatility_20", "Downside volatility", "Risk", 20, "long_low"),
            ("average_traded_value_20", "Average traded value", "Liquidity", 20, "long_high"),
            ("amihud_illiquidity_20", "Amihud illiquidity", "Liquidity", 20, "long_low"),
            ("volume_activity_proxy_20", "Volume activity", "Volume", 20, "long_high"),
            ("volume_surprise_20", "Volume surprise", "Volume", 20, "long_high"),
            ("distance_from_ma_20", "Distance from MA", "Trend", 20, "long_high"),
            ("distance_from_rolling_high_20", "Distance from high", "Trend", 20, "long_high"),
            ("close_location_value", "Close location value", "Price", 1, "long_high"),
        )
        return tuple(
            FactorSummary(
                factor_id=factor_id,
                name=name,
                version="1.0.0",
                category=category,
                lookback=lookback,
                coverage=round(0.79 + (index % 6) * 0.025, 3),
                mean_ic=round(-0.025 + index * 0.004, 4),
                rank_ic=round(-0.018 + index * 0.0035, 4),
                icir=None if index < 2 else round(-0.35 + index * 0.07, 3),
                turnover=round(0.12 + (index % 5) * 0.07, 3),
                direction=direction,
            )
            for index, (factor_id, name, category, lookback, direction) in enumerate(definitions)
        )

    def _factor_detail(self, summary: FactorSummary) -> FactorDetail:
        offset = list(self.factors).index(summary)
        return FactorDetail(
            context=self.context,
            summary=summary,
            mathematical_definition=(
                "Synthetic demonstration definition. Production formula remains pinned in the "
                "versioned feature contract."
            ),
            required_columns=("open", "high", "low", "close", "volume", "traded_value"),
            price_view="canonical_unadjusted",
            minimum_observations=max(
                2, summary.lookback + (1 if "volatility" in summary.factor_id else 0)
            ),
            missing_policy="fail closed; no implicit zero fill",
            normalization="daily eligible-universe cross-sectional rank",
            availability_policy=AVAILABILITY_VERSION,
            dataset_version=DATASET_VERSION,
            feature_artifact_version=f"demo-{summary.factor_id}-artifact-v1",
            daily_ic=_series(
                "Daily rank IC",
                "#74a7d8",
                [math.sin((index + offset) / 4) * 0.12 for index in range(45)],
            ),
            ic_decay=_series(
                "IC decay",
                "#b48dd1",
                [summary.rank_ic * math.exp(-index / 3) for index in range(1, 9)],
                "H",
            ),
            quantile_returns=_series(
                "Gross return",
                "#65b88c",
                [-0.014, -0.006, 0.001, 0.008, 0.017],
                "Q",
            ),
            coverage_series=_series(
                "Coverage",
                "#d9a441",
                [summary.coverage * 100 + math.sin(i / 5) for i in range(30)],
            ),
            distribution=_series(
                "Observations",
                "#6f87a5",
                [2, 7, 18, 31, 45, 33, 17, 6, 2],
                "B",
            ),
            breakdown=(
                {"segment": "2024 demo", "rank_ic": f"{summary.rank_ic:.4f}", "coverage": "86%"},
                {"segment": "TWSE synthetic", "rank_ic": "0.0310", "coverage": "88%"},
                {"segment": "TPEx synthetic", "rank_ic": "0.0190", "coverage": "83%"},
            ),
            known_limitations=(
                "Synthetic sample is too short for inference.",
                "No transaction costs, taxes, or executable portfolio simulation.",
                "Historical listing and suspension references are incomplete.",
            ),
            warning=WARNING,
            fixture_label="Synthetic fixture · Exploratory · Gross before costs",
        )

    def _experiments(self) -> tuple[ExperimentSummary, ...]:
        return (
            ExperimentSummary(
                experiment_id="demo-exp-linear-v1",
                created_at=datetime(2024, 5, 10, 12, tzinfo=UTC),
                target="Raw O2O return",
                model="Linear regression",
                fold_count=3,
                feature_set=FEATURE_VERSION,
                validation_rank_ic=1.0,
                test_rank_ic=1.0,
                mae=0.00142,
                rmse=0.00345,
            ),
            ExperimentSummary(
                experiment_id="demo-exp-ridge-v1",
                created_at=datetime(2024, 5, 9, 12, tzinfo=UTC),
                target="Benchmark-relative return",
                model="Ridge α=1.0",
                fold_count=3,
                feature_set=FEATURE_VERSION,
                validation_rank_ic=0.124,
                test_rank_ic=0.087,
                mae=0.0082,
                rmse=0.0127,
            ),
            ExperimentSummary(
                experiment_id="demo-exp-elastic-v1",
                created_at=datetime(2024, 5, 8, 12, tzinfo=UTC),
                target="Cross-sectional rank",
                model="Elastic Net",
                fold_count=4,
                feature_set=FEATURE_VERSION,
                validation_rank_ic=0.091,
                test_rank_ic=0.061,
                mae=0.108,
                rmse=0.141,
            ),
        )

    def _experiment_detail(
        self, summary: ExperimentSummary, offset: int
    ) -> ExperimentDetail:
        folds = tuple(
            FoldMetric(
                fold_id=f"fold-{index:03d}",
                train=f"2024-01-02 — 2024-0{index + 1}-02",
                validation=f"2024-0{index + 1}-05 — 2024-0{index + 1}-12",
                test=f"2024-0{index + 1}-15 — 2024-0{index + 1}-26",
                validation_rank_ic=round(summary.validation_rank_ic - index * 0.004, 4),
                test_rank_ic=round(summary.test_rank_ic - index * 0.003, 4),
                purged=index,
                embargoed=15,
            )
            for index in range(1, summary.fold_count + 1)
        )
        return ExperimentDetail(
            context=self.context,
            summary=summary,
            model_card={
                "Intended use": "Offline baseline research",
                "Not intended use": "Live trading or investment advice",
                "Return semantics": "Gross before costs",
                "Completeness": "EXPLORATORY — INCOMPLETE POINT-IN-TIME REFERENCE DATA",
            },
            versions={
                "Dataset": DATASET_VERSION,
                "Features": FEATURE_VERSION,
                "Labels": LABEL_VERSION,
                "Availability": AVAILABILITY_VERSION,
            },
            folds=folds,
            purge_count=sum(item.purged for item in folds),
            embargo_count=sum(item.embargoed for item in folds),
            sample_weight_policy="Equal total weight per decision date",
            hyperparameters={
                "alpha": "0.0" if offset == 0 else "1.0",
                "random_seed": str(DEMO_SEED),
                "selection_metric": "Validation mean daily Spearman IC",
            },
            prediction_distribution=_series(
                "Predictions", "#74a7d8", [3, 9, 23, 42, 56, 39, 18, 7, 2], "B"
            ),
            daily_rank_ic=_series(
                "Daily OOS rank IC",
                "#65b88c",
                ([1.0] * 30 if offset == 0 else [math.sin(i / 3) * 0.18 for i in range(30)]),
            ),
            quantile_returns=_series(
                "Gross return", "#b48dd1", [-0.013, -0.004, 0.002, 0.009, 0.019], "Q"
            ),
            bootstrap_interval=(1.0, 1.0) if offset == 0 else (-0.04, 0.15),
            bootstrap_config={"seed": DEMO_SEED, "block_length": 5, "resamples": 100},
            holdout_access_count=0,
            known_limitations=(
                "Synthetic demo data only.",
                "No real holdout validation or transaction costs.",
                "No event-driven backtest has been performed.",
            ),
            synthetic_warning=SYNTHETIC_WARNING,
            promotion_reasons=(
                "Incomplete PIT data",
                "Demo fixture",
                "No real holdout validation",
                "No transaction costs",
                "No backtest",
            ),
        )

    def backtests(self) -> BacktestView:
        planned = (
            "Event-driven engine",
            "Portfolio accounting",
            "Fees and taxes",
            "Settlement",
            "Fill simulation",
            "Risk controls",
            "Deterministic replay",
        )
        return BacktestView(
            context=self.context,
            planned_capabilities=planned,
            metrics={name: "Not available" for name in ("Equity", "PnL", "Sharpe", "Drawdown")},
        )

    def system(self) -> SystemView:
        return SystemView(
            context=self.context,
            package_version="0.1.0",
            python_version=platform.python_version(),
            runtime_environment="Local demo fixture",
            git_commit_short="f0fb2bf",
            dirty=True,
            source_tree_hash_short="demo9c13",
            dataset_version=DATASET_VERSION,
            feature_set_version=FEATURE_VERSION,
            label_version=LABEL_VERSION,
            availability_policy_version=AVAILABILITY_VERSION,
            docker_status="Available via documented smoke test",
            checks=(
                HealthCheck(name="Application", status="ok", detail="FastAPI read-only routes"),
                HealthCheck(
                    name="Artifact reader", status="demo", detail="Synthetic fixture adapter"
                ),
                HealthCheck(name="Configuration", status="ok", detail="Safe local defaults"),
                HealthCheck(name="Fixture provider", status="ok", detail=f"Fixed seed {DEMO_SEED}"),
                HealthCheck(name="Live trading", status="disabled", detail="Hard disabled"),
            ),
        )
