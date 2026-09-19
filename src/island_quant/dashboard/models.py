"""Typed dashboard view models, deliberately separate from research domain objects."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ViewModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChartPoint(ViewModel):
    label: str
    value: float


class ChartSeries(ViewModel):
    name: str
    color: str
    points: tuple[ChartPoint, ...]


class DashboardContext(ViewModel):
    project_name: str = "Island Quant"
    environment: Literal["DEMO", "ARTIFACT", "PIPELINE"] = "DEMO"
    banner: str = "DEMO / EXPLORATORY — NOT FOR LIVE TRADING"
    dataset_version: str
    pit_completeness: Literal["incomplete", "validated"] = "incomplete"
    last_artifact_update: datetime
    universes: tuple[str, ...]
    selected_universe: str
    selected_date_range: str


class SummaryCard(ViewModel):
    label: str
    value: str
    status: Literal["neutral", "positive", "warning", "negative"] = "neutral"
    note: str | None = None


class PipelineStage(ViewModel):
    order: int
    name: str
    status: Literal["Complete", "Exploratory", "Blocked", "Not started"]
    detail: str


class ActivityEvent(ViewModel):
    created_at: datetime
    kind: str
    message: str
    is_demo: Literal[True] = True


class OverviewView(ViewModel):
    context: DashboardContext
    cards: tuple[SummaryCard, ...]
    pipeline: tuple[PipelineStage, ...]
    coverage: ChartSeries
    feature_validity: tuple[ChartSeries, ...]
    daily_samples: ChartSeries
    quality_categories: ChartSeries
    recent_activity: tuple[ActivityEvent, ...]


IssueSeverity = Literal["info", "warning", "error"]
ResolutionStatus = Literal["open", "documented", "fixture-only"]


class DataIssue(ViewModel):
    issue_id: str
    instrument_id: str
    event_date: date
    category: str
    severity: IssueSeverity
    reason: str
    source: str
    resolution_status: ResolutionStatus


class DataIssuePage(ViewModel):
    items: tuple[DataIssue, ...]
    page: int
    page_size: int
    total: int
    pages: int


class DataHealthView(ViewModel):
    context: DashboardContext
    dataset_version: str
    raw_source: str
    date_range: str
    instruments: int
    rows: int
    checksum_short: str
    provenance: Literal["clean", "dirty"]
    availability_policy: str
    completeness: Literal["exploratory"]
    issue_counts: dict[str, int]
    issues: DataIssuePage


class FactorSummary(ViewModel):
    factor_id: str
    name: str
    version: str
    category: str
    lookback: int
    coverage: float
    mean_ic: float
    rank_ic: float
    icir: float | None
    turnover: float
    direction: str
    status: Literal["Exploratory"] = "Exploratory"
    return_semantics: Literal["gross-before-costs"] = "gross-before-costs"


class FactorDetail(ViewModel):
    context: DashboardContext
    summary: FactorSummary
    mathematical_definition: str
    required_columns: tuple[str, ...]
    price_view: str
    minimum_observations: int
    missing_policy: str
    normalization: str
    availability_policy: str
    dataset_version: str
    feature_artifact_version: str
    daily_ic: ChartSeries
    ic_decay: ChartSeries
    quantile_returns: ChartSeries
    coverage_series: ChartSeries
    distribution: ChartSeries
    breakdown: tuple[dict[str, str], ...]
    known_limitations: tuple[str, ...]
    warning: str
    fixture_label: str


class FactorListView(ViewModel):
    context: DashboardContext
    items: tuple[FactorSummary, ...]
    categories: tuple[str, ...]


class FoldMetric(ViewModel):
    fold_id: str
    train: str
    validation: str
    test: str
    validation_rank_ic: float
    test_rank_ic: float
    purged: int
    embargoed: int


class ExperimentSummary(ViewModel):
    experiment_id: str
    created_at: datetime
    target: str
    model: str
    fold_count: int
    feature_set: str
    validation_rank_ic: float
    test_rank_ic: float
    mae: float
    rmse: float
    completeness: Literal["Exploratory"] = "Exploratory"
    status: Literal["Candidate"] = "Candidate"


class ExperimentDetail(ViewModel):
    context: DashboardContext
    summary: ExperimentSummary
    model_card: dict[str, str]
    versions: dict[str, str]
    folds: tuple[FoldMetric, ...]
    purge_count: int
    embargo_count: int
    sample_weight_policy: str
    hyperparameters: dict[str, str]
    prediction_distribution: ChartSeries
    daily_rank_ic: ChartSeries
    quantile_returns: ChartSeries
    bootstrap_interval: tuple[float, float]
    bootstrap_config: dict[str, int]
    holdout_access_count: int
    known_limitations: tuple[str, ...]
    synthetic_warning: str
    promotion_enabled: Literal[False] = False
    promotion_reasons: tuple[str, ...]


class ExperimentListView(ViewModel):
    context: DashboardContext
    items: tuple[ExperimentSummary, ...]


class BacktestView(ViewModel):
    context: DashboardContext
    implemented: Literal[False] = False
    title: str = "Backtesting is not implemented yet"
    planned_capabilities: tuple[str, ...]
    metrics: dict[str, Literal["Not available"]]


class HealthCheck(ViewModel):
    name: str
    status: Literal["ok", "demo", "disabled"]
    detail: str


class SystemView(ViewModel):
    context: DashboardContext
    package_version: str
    python_version: str
    runtime_environment: str
    git_commit_short: str
    dirty: bool
    source_tree_hash_short: str
    dataset_version: str
    feature_set_version: str
    label_version: str
    availability_policy_version: str
    docker_status: str
    live_trading_enabled: Literal[False] = False
    broker_configured: Literal[False] = False
    checks: tuple[HealthCheck, ...]


class HealthResponse(ViewModel):
    status: Literal["ok"] = "ok"
    mode: Literal["demo-read-only", "artifact-read-only", "pipeline-read-only"] = "demo-read-only"
    live_trading_enabled: Literal[False] = False


class PaginatedFactors(ViewModel):
    items: tuple[FactorSummary, ...]
    page: int
    page_size: int
    total: int


class PaginatedExperiments(ViewModel):
    items: tuple[ExperimentSummary, ...]
    page: int
    page_size: int
    total: int
