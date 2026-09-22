"""FastAPI composition root for the local read-only research dashboard."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParameter
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from island_quant.backtest.artifacts import VERSION_PATTERN
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

    @application.middleware("http")
    async def reject_invalid_artifact_paths(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        from urllib.parse import unquote

        raw = request.scope.get("raw_path", b"").decode("ascii", errors="ignore")
        for prefix in (
            "/api/dashboard/backtests/",
            "/backtests/",
            "/api/dashboard/pipelines/",
            "/pipelines/",
        ):
            if raw.startswith(prefix):
                encoded_version = raw[len(prefix) :].split("/", 1)[0]
                if not VERSION_PATTERN.fullmatch(unquote(encoded_version)):
                    return JSONResponse(
                        status_code=422,
                        content={"detail": "invalid artifact version format"},
                    )
        return await call_next(request)

    def page(request: Request, template: str, model: object, active: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name=template,
            context={"view": model, "active": active},
        )

    def demo_only() -> None:
        if query.artifact_mode or query.pipeline_mode or query.operations_mode:
            raise HTTPException(
                status_code=404,
                detail="Demo fixture endpoint is unavailable in artifact mode",
            )

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    def overview_page(request: Request) -> HTMLResponse:
        if query.operations_query is not None:
            return page(
                request,
                "paper_operations.html",
                query.operations_query.status(datetime.now().astimezone()),
                "paper-operations",
            )
        demo_only()
        return page(request, "overview.html", query.overview(), "overview")

    @application.get("/data-health", response_class=HTMLResponse, include_in_schema=False)
    def data_health_page(request: Request) -> HTMLResponse:
        demo_only()
        return page(request, "data_health.html", query.data_health(), "data-health")

    @application.get("/factors", response_class=HTMLResponse, include_in_schema=False)
    def factors_page(request: Request) -> HTMLResponse:
        demo_only()
        return page(request, "factors.html", query.factors(), "factors")

    @application.get("/factors/{factor_id}", response_class=HTMLResponse, include_in_schema=False)
    def factor_page(request: Request, factor_id: str) -> HTMLResponse:
        demo_only()
        detail = query.factor(factor_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Unknown factor")
        return page(request, "factor_detail.html", detail, "factors")

    @application.get("/experiments", response_class=HTMLResponse, include_in_schema=False)
    def experiments_page(request: Request) -> HTMLResponse:
        demo_only()
        return page(request, "experiments.html", query.experiments(), "experiments")

    @application.get(
        "/experiments/{experiment_id}", response_class=HTMLResponse, include_in_schema=False
    )
    def experiment_page(request: Request, experiment_id: str) -> HTMLResponse:
        demo_only()
        detail = query.experiment(experiment_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Unknown experiment")
        return page(request, "experiment_detail.html", detail, "experiments")

    @application.get("/backtests", response_class=HTMLResponse, include_in_schema=False)
    def backtests_page(request: Request) -> HTMLResponse:
        if query.pipeline_mode and query.backtest_query is None:
            raise HTTPException(status_code=404, detail="Pipeline has no backtest artifact")
        template = "backtest_artifacts.html" if query.artifact_mode else "backtests.html"
        if query.pipeline_mode:
            template = "backtest_artifacts.html"
        return page(request, template, query.backtests(), "backtests")

    @application.get("/pipelines/{version}", response_class=HTMLResponse, include_in_schema=False)
    def pipeline_page(
        request: Request,
        version: Annotated[str, PathParameter(pattern=r"^[0-9a-f]{64}$")],
    ) -> HTMLResponse:
        if query.pipeline_query is None or version != query.pipeline_query.version:
            raise HTTPException(status_code=404, detail="Unknown pipeline artifact")
        return page(request, "pipeline_status.html", query.pipeline(), "pipeline")

    @application.get("/backtests/{version}", response_class=HTMLResponse, include_in_schema=False)
    def backtest_page(
        request: Request,
        version: Annotated[str, PathParameter(pattern=r"^[0-9a-f]{64}$")],
    ) -> HTMLResponse:
        detail = query.backtest(version)
        if detail is None:
            raise HTTPException(status_code=404, detail="Unknown backtest artifact")
        return page(request, "backtest_detail.html", detail, "backtests")

    @application.get("/system", response_class=HTMLResponse, include_in_schema=False)
    def system_page(request: Request) -> HTMLResponse:
        demo_only()
        return page(request, "system.html", query.system(), "system")

    @application.get("/api/dashboard/overview", response_model=OverviewView)
    def overview_api() -> OverviewView:
        demo_only()
        return query.overview()

    @application.get("/api/dashboard/data-health", response_model=DataHealthView)
    def data_health_api() -> DataHealthView:
        demo_only()
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
        demo_only()
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
        demo_only()
        return query.paginated_factors(category, page, page_size)

    @application.get("/api/dashboard/factors/{factor_id}", response_model=FactorDetail)
    def factor_api(factor_id: str) -> FactorDetail:
        demo_only()
        detail = query.factor(factor_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Unknown factor")
        return detail

    @application.get("/api/dashboard/experiments", response_model=PaginatedExperiments)
    def experiments_api(
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> PaginatedExperiments:
        demo_only()
        return query.paginated_experiments(page, page_size)

    @application.get("/api/dashboard/experiments/{experiment_id}", response_model=ExperimentDetail)
    def experiment_api(experiment_id: str) -> ExperimentDetail:
        demo_only()
        detail = query.experiment(experiment_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Unknown experiment")
        return detail

    @application.get("/api/dashboard/system", response_model=SystemView)
    def system_api() -> SystemView:
        demo_only()
        return query.system()

    @application.get("/api/dashboard/paper-operations")
    def paper_operations_api() -> object:
        if query.operations_query is None:
            raise HTTPException(status_code=404, detail="Paper operations mode is not configured")
        return query.operations_query.status(datetime.now().astimezone())

    @application.get("/api/dashboard/backtests")
    def backtests_api() -> dict[str, object]:
        if query.backtest_query is None:
            if query.pipeline_mode:
                raise HTTPException(status_code=404, detail="Pipeline has no backtest artifact")
            return {"mode": "demo", "artifact_version": None, "items": []}
        return {
            "mode": "pipeline" if query.pipeline_mode else "artifact",
            "artifact_version": query.backtest_query.version,
            "items": query.backtest_query.summaries(),
        }

    @application.get("/api/dashboard/pipelines/{version}")
    def pipeline_api(
        version: Annotated[str, PathParameter(pattern=r"^[0-9a-f]{64}$")],
    ) -> object:
        if query.pipeline_query is None or version != query.pipeline_query.version:
            raise HTTPException(status_code=404, detail="Unknown pipeline artifact")
        return query.pipeline_query.status()

    @application.get("/api/dashboard/", include_in_schema=False)
    def rejected_normalized_artifact_path() -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": "invalid artifact version format"},
        )

    def required(value: object | None) -> object:
        if value is None:
            raise HTTPException(status_code=404, detail="Unknown backtest artifact")
        if query.pipeline_mode and isinstance(value, dict):
            return {**value, "mode": "pipeline"}
        return value

    @application.get("/api/dashboard/backtests/{version}")
    def backtest_api(
        version: Annotated[str, PathParameter(pattern=r"^[0-9a-f]{64}$")],
    ) -> object:
        return required(
            query.backtest_query.detail(version) if query.backtest_query is not None else None
        )

    @application.get("/api/dashboard/backtests/{version}/equity")
    def backtest_equity_api(
        version: Annotated[str, PathParameter(pattern=r"^[0-9a-f]{64}$")],
    ) -> object:
        return required(
            query.backtest_query.equity(version) if query.backtest_query is not None else None
        )

    @application.get("/api/dashboard/backtests/{version}/attribution")
    def backtest_attribution_api(
        version: Annotated[str, PathParameter(pattern=r"^[0-9a-f]{64}$")],
    ) -> object:
        return required(
            query.backtest_query.attribution(version) if query.backtest_query is not None else None
        )

    @application.get("/api/dashboard/backtests/{version}/orders")
    def backtest_orders_api(
        version: Annotated[str, PathParameter(pattern=r"^[0-9a-f]{64}$")],
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> object:
        return required(
            query.backtest_query.orders(version, page, page_size)
            if query.backtest_query is not None
            else None
        )

    @application.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        mode: Literal[
            "demo-read-only", "artifact-read-only", "pipeline-read-only", "paper-read-only"
        ]
        if query.operations_mode:
            mode = "paper-read-only"
        elif query.pipeline_mode:
            mode = "pipeline-read-only"
        elif query.artifact_mode:
            mode = "artifact-read-only"
        else:
            mode = "demo-read-only"
        return HealthResponse(mode=mode)

    return application


app = create_app()
