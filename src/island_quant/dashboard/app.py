"""FastAPI composition root for the local read-only research dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from island_quant.dashboard.models import (
    DataHealthView,
    DataIssuePage,
    ExperimentDetail,
    FactorDetail,
    HealthResponse,
    OverviewView,
    PaginatedExperiments,
    PaginatedFactors,
    SystemView,
)
from island_quant.dashboard.service import DashboardQueryService

PACKAGE_ROOT = Path(__file__).resolve().parent


def create_app(service: DashboardQueryService | None = None) -> FastAPI:
    query = service or DashboardQueryService()
    application = FastAPI(
        title="Island Quant Research Dashboard",
        description="Read-only synthetic research dashboard; not for live trading.",
        version="0.1.0",
    )
    templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")
    application.mount(
        "/assets", StaticFiles(directory=PACKAGE_ROOT / "static"), name="dashboard-assets"
    )

    def page(request: Request, template: str, model: object, active: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name=template,
            context={"view": model, "active": active},
        )

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    def overview_page(request: Request) -> HTMLResponse:
        return page(request, "overview.html", query.overview(), "overview")

    @application.get("/data-health", response_class=HTMLResponse, include_in_schema=False)
    def data_health_page(request: Request) -> HTMLResponse:
        return page(request, "data_health.html", query.data_health(), "data-health")

    @application.get("/factors", response_class=HTMLResponse, include_in_schema=False)
    def factors_page(request: Request) -> HTMLResponse:
        return page(request, "factors.html", query.factors(), "factors")

    @application.get(
        "/factors/{factor_id}", response_class=HTMLResponse, include_in_schema=False
    )
    def factor_page(request: Request, factor_id: str) -> HTMLResponse:
        detail = query.factor(factor_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Unknown factor")
        return page(request, "factor_detail.html", detail, "factors")

    @application.get("/experiments", response_class=HTMLResponse, include_in_schema=False)
    def experiments_page(request: Request) -> HTMLResponse:
        return page(request, "experiments.html", query.experiments(), "experiments")

    @application.get(
        "/experiments/{experiment_id}", response_class=HTMLResponse, include_in_schema=False
    )
    def experiment_page(request: Request, experiment_id: str) -> HTMLResponse:
        detail = query.experiment(experiment_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Unknown experiment")
        return page(request, "experiment_detail.html", detail, "experiments")

    @application.get("/backtests", response_class=HTMLResponse, include_in_schema=False)
    def backtests_page(request: Request) -> HTMLResponse:
        return page(request, "backtests.html", query.fixture.backtests(), "backtests")

    @application.get("/system", response_class=HTMLResponse, include_in_schema=False)
    def system_page(request: Request) -> HTMLResponse:
        return page(request, "system.html", query.system(), "system")

    @application.get("/api/dashboard/overview", response_model=OverviewView)
    def overview_api() -> OverviewView:
        return query.overview()

    @application.get("/api/dashboard/data-health", response_model=DataHealthView)
    def data_health_api() -> DataHealthView:
        return query.data_health()

    @application.get("/api/dashboard/data-issues", response_model=DataIssuePage)
    def data_issues_api(
        category: str | None = None,
        severity: Literal["info", "warning", "error"] | None = None,
        instrument: Annotated[str | None, Query(max_length=20)] = None,
        start_date: str | None = None,
        end_date: str | None = None,
        sort: Literal["date_desc", "date_asc", "severity"] = "date_desc",
        page_number: Annotated[int, Query(alias="page", ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> DataIssuePage:
        return query.data_issues(
            category=category,
            severity=severity,
            instrument=instrument,
            start_date=start_date,
            end_date=end_date,
            sort=sort,
            page=page_number,
            page_size=page_size,
        )

    @application.get("/api/dashboard/factors", response_model=PaginatedFactors)
    def factors_api(
        category: str | None = None,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> PaginatedFactors:
        return query.paginated_factors(category, page, page_size)

    @application.get("/api/dashboard/factors/{factor_id}", response_model=FactorDetail)
    def factor_api(factor_id: str) -> FactorDetail:
        detail = query.factor(factor_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Unknown factor")
        return detail

    @application.get("/api/dashboard/experiments", response_model=PaginatedExperiments)
    def experiments_api(
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> PaginatedExperiments:
        return query.paginated_experiments(page, page_size)

    @application.get(
        "/api/dashboard/experiments/{experiment_id}", response_model=ExperimentDetail
    )
    def experiment_api(experiment_id: str) -> ExperimentDetail:
        detail = query.experiment(experiment_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Unknown experiment")
        return detail

    @application.get("/api/dashboard/system", response_model=SystemView)
    def system_api() -> SystemView:
        return query.system()

    @application.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    return application


app = create_app()
