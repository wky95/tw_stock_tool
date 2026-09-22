"""Read-only application query service for dashboard view models."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from island_quant.dashboard.backtests import BacktestArtifactQuery
from island_quant.dashboard.fixture import DemoDashboardFixture
from island_quant.dashboard.models import (
    DashboardContext,
    DataHealthView,
    DataIssuePage,
    ExperimentDetail,
    ExperimentListView,
    FactorDetail,
    FactorListView,
    OverviewView,
    PaginatedExperiments,
    PaginatedFactors,
    SystemView,
)
from island_quant.dashboard.operations import PaperOperationsQuery
from island_quant.dashboard.pipelines import PipelineArtifactQuery
from island_quant.storage.provenance import CodeProvenance, capture_code_provenance


class DashboardQueryService:
    def __init__(
        self,
        fixture: DemoDashboardFixture | None = None,
        provenance_provider: Callable[[], CodeProvenance] = capture_code_provenance,
        backtest_query: BacktestArtifactQuery | None = None,
        pipeline_query: PipelineArtifactQuery | None = None,
        operations_query: PaperOperationsQuery | None = None,
    ) -> None:
        self.fixture = fixture or DemoDashboardFixture()
        self.provenance_provider = provenance_provider
        self.backtest_query = backtest_query
        self.pipeline_query = pipeline_query
        self.operations_query = operations_query

    @property
    def artifact_mode(self) -> bool:
        return self.backtest_query is not None and self.pipeline_query is None

    @property
    def pipeline_mode(self) -> bool:
        return self.pipeline_query is not None

    @property
    def operations_mode(self) -> bool:
        return self.operations_query is not None

    def pipeline_context(self) -> DashboardContext:
        if self.pipeline_query is None:
            raise RuntimeError("pipeline mode is not configured")
        status = self.pipeline_query.status()
        coverage = status["coverage"]
        return DashboardContext(
            environment="PIPELINE",
            banner="REAL EXPLORATORY / PIT INCOMPLETE / READ ONLY / NOT FOR LIVE TRADING",
            dataset_version=self.pipeline_query.version[:12],
            pit_completeness="incomplete",
            last_artifact_update=self.fixture.context.last_artifact_update,
            universes=("Pinned exploratory universe",),
            selected_universe="Pinned exploratory universe",
            selected_date_range=str(coverage["date_coverage"]),
        )

    def pipeline(self) -> dict[str, object]:
        if self.pipeline_query is None:
            raise RuntimeError("pipeline mode is not configured")
        return {"context": self.pipeline_context(), **self.pipeline_query.status()}

    def backtest_context(self) -> DashboardContext:
        if self.backtest_query is None:
            return self.fixture.context
        summary = self.backtest_query.summaries()[0]
        banner_parts = ["READ-ONLY BACKTEST ARTIFACT"]
        if summary["synthetic_demo"]:
            banner_parts.append("SYNTHETIC")
        banner_parts.append(str(summary["classification"]).upper())
        if summary["completeness_status"] != "validated":
            banner_parts.append("PIT INCOMPLETE")
        if summary["dirty"]:
            banner_parts.append("DIRTY SOURCE TREE")
        banner_parts.append("NOT FOR LIVE TRADING")
        return DashboardContext(
            environment="ARTIFACT",
            banner=" / ".join(banner_parts),
            dataset_version=str(summary["artifact_version"])[:12],
            pit_completeness=(
                "validated" if summary["completeness_status"] == "validated" else "incomplete"
            ),
            last_artifact_update=datetime.fromisoformat(str(summary["created_time"])),
            universes=("Pinned artifact universe",),
            selected_universe="Pinned artifact universe",
            selected_date_range=" to ".join(summary["date_range"]),
        )

    def backtests(self) -> object:
        if self.backtest_query is None:
            if self.pipeline_query is not None:
                raise RuntimeError("pipeline run has no verified backtest artifact")
            return self.fixture.backtests()
        return {
            "context": (
                self.pipeline_context() if self.pipeline_query else self.backtest_context()
            ),
            "items": self.backtest_query.summaries(),
        }

    def backtest(self, version: str) -> dict[str, object] | None:
        if self.backtest_query is None:
            return None
        detail = self.backtest_query.detail(version)
        if detail is None:
            return None
        context = self.pipeline_context() if self.pipeline_query else self.backtest_context()
        return {"context": context, **detail}

    def overview(self) -> OverviewView:
        source = self.fixture
        return OverviewView(
            context=source.context,
            cards=source.overview_cards(),
            pipeline=source.pipeline(),
            coverage=source.coverage_chart(),
            feature_validity=source.validity_charts(),
            daily_samples=source.sample_chart(),
            quality_categories=source.quality_chart(),
            recent_activity=source.activities(),
        )

    def data_issues(
        self,
        *,
        category: str | None = None,
        severity: Literal["info", "warning", "error"] | None = None,
        instrument: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        sort: Literal["date_desc", "date_asc", "severity"] = "date_desc",
        page: int = 1,
        page_size: int = 20,
    ) -> DataIssuePage:
        items = list(self.fixture.issues)
        if category:
            items = [item for item in items if item.category == category]
        if severity:
            items = [item for item in items if item.severity == severity]
        if instrument:
            query = instrument.upper()
            items = [item for item in items if query in item.instrument_id.upper()]
        if start_date:
            items = [item for item in items if item.event_date.isoformat() >= start_date]
        if end_date:
            items = [item for item in items if item.event_date.isoformat() <= end_date]
        severity_order = {"error": 0, "warning": 1, "info": 2}
        if sort == "severity":
            items.sort(
                key=lambda item: (
                    severity_order[item.severity],
                    -item.event_date.toordinal(),
                )
            )
        else:
            items.sort(key=lambda item: item.event_date, reverse=sort == "date_desc")
        total = len(items)
        start = (page - 1) * page_size
        return DataIssuePage(
            items=tuple(items[start : start + page_size]),
            page=page,
            page_size=page_size,
            total=total,
            pages=max(1, math.ceil(total / page_size)),
        )

    def data_health(self, issues: DataIssuePage | None = None) -> DataHealthView:
        selected = issues or self.data_issues()
        counts: dict[str, int] = {}
        for item in self.fixture.issues:
            counts[item.category] = counts.get(item.category, 0) + 1
        return DataHealthView(
            context=self.fixture.context,
            dataset_version=self.fixture.context.dataset_version,
            raw_source="Synthetic deterministic fixture",
            date_range=self.fixture.context.selected_date_range,
            instruments=self.fixture.instrument_count,
            rows=self.fixture.instrument_count * self.fixture.session_count,
            checksum_short="d3m09c13f7a2",
            provenance="clean",
            availability_policy="tw-daily-demo-v1",
            completeness="exploratory",
            issue_counts=counts,
            issues=selected,
        )

    def factors(self, category: str | None = None) -> FactorListView:
        items = self.fixture.factors
        if category:
            items = tuple(item for item in items if item.category == category)
        return FactorListView(
            context=self.fixture.context,
            items=items,
            categories=tuple(sorted({item.category for item in self.fixture.factors})),
        )

    def paginated_factors(
        self, category: str | None, page: int, page_size: int
    ) -> PaginatedFactors:
        items = self.factors(category).items
        start = (page - 1) * page_size
        return PaginatedFactors(
            items=items[start : start + page_size], page=page, page_size=page_size, total=len(items)
        )

    def factor(self, factor_id: str) -> FactorDetail | None:
        return self.fixture.factor_details.get(factor_id)

    def experiments(self) -> ExperimentListView:
        return ExperimentListView(context=self.fixture.context, items=self.fixture.experiments)

    def paginated_experiments(self, page: int, page_size: int) -> PaginatedExperiments:
        items = self.fixture.experiments
        start = (page - 1) * page_size
        return PaginatedExperiments(
            items=items[start : start + page_size], page=page, page_size=page_size, total=len(items)
        )

    def experiment(self, experiment_id: str) -> ExperimentDetail | None:
        return self.fixture.experiment_details.get(experiment_id)

    def system(self) -> SystemView:
        view = self.fixture.system()
        try:
            provenance = self.provenance_provider()
        except RuntimeError:
            return view.model_copy(
                update={
                    "git_commit_short": "unavailable",
                    "dirty": True,
                    "source_tree_hash_short": "unavailable",
                }
            )
        return view.model_copy(
            update={
                "git_commit_short": provenance.git_commit[:8],
                "dirty": provenance.dirty,
                "source_tree_hash_short": provenance.source_tree_hash[:8],
            }
        )
