from __future__ import annotations

import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from island_quant.cli import main
from island_quant.dashboard.app import create_app
from island_quant.dashboard.fixture import DemoDashboardFixture


def client() -> TestClient:
    return TestClient(create_app())


def test_dashboard_fixture_is_deterministic() -> None:
    first = DemoDashboardFixture()
    second = DemoDashboardFixture()
    assert first.seed == second.seed == 20240918
    assert first.context == second.context
    assert first.factors == second.factors
    assert first.experiment_details == second.experiment_details
    assert first.issues == second.issues


def test_all_read_only_api_endpoints_are_available() -> None:
    browser = client()
    endpoints = (
        "/api/dashboard/overview",
        "/api/dashboard/data-health",
        "/api/dashboard/data-issues",
        "/api/dashboard/factors",
        "/api/dashboard/factors/momentum_5",
        "/api/dashboard/experiments",
        "/api/dashboard/experiments/demo-exp-linear-v1",
        "/api/dashboard/system",
        "/health",
        "/docs",
        "/openapi.json",
    )
    for endpoint in endpoints:
        assert browser.get(endpoint).status_code == 200, endpoint
    paths = browser.get("/openapi.json").json()["paths"]
    assert all(set(operations) <= {"get"} for operations in paths.values())


def test_unknown_factor_and_experiment_return_404() -> None:
    browser = client()
    assert browser.get("/api/dashboard/factors/not-real").status_code == 404
    assert browser.get("/api/dashboard/experiments/not-real").status_code == 404
    assert browser.get("/factors/not-real").status_code == 404
    assert browser.get("/experiments/not-real").status_code == 404


def test_invalid_filters_return_422() -> None:
    browser = client()
    assert browser.get("/api/dashboard/data-issues?severity=critical").status_code == 422
    assert browser.get("/api/dashboard/data-issues?page=0").status_code == 422
    assert browser.get("/api/dashboard/factors?page_size=101").status_code == 422


def test_issue_filtering_pagination_and_search() -> None:
    response = client().get(
        "/api/dashboard/data-issues?severity=error&instrument=D100&page_size=2&sort=severity"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["page_size"] == 2
    assert all(item["severity"] == "error" for item in payload["items"])
    assert all("D100" in item["instrument_id"] for item in payload["items"])


def test_view_models_do_not_leak_secrets_credentials_or_paths() -> None:
    browser = client()
    payloads = [
        browser.get("/api/dashboard/overview").json(),
        browser.get("/api/dashboard/system").json(),
        browser.get("/api/dashboard/data-health").json(),
    ]
    encoded = json.dumps(payloads).lower()
    assert "api_key" not in encoded
    assert "credential" not in encoded
    assert "/users/" not in encoded
    assert "wangkaiyu" not in encoded


def test_demo_badge_and_safety_banner_exist_on_every_page() -> None:
    browser = client()
    pages = (
        "/",
        "/data-health",
        "/factors",
        "/factors/momentum_5",
        "/experiments",
        "/experiments/demo-exp-linear-v1",
        "/backtests",
        "/system",
    )
    for page in pages:
        body = browser.get(page).text
        assert "DEMO / EXPLORATORY — NOT FOR LIVE TRADING" in body
        assert "Synthetic fixture" in body


def test_live_trading_and_broker_are_always_false() -> None:
    payload = client().get("/api/dashboard/system").json()
    assert payload["live_trading_enabled"] is False
    assert payload["broker_configured"] is False
    health = client().get("/health").json()
    assert health["live_trading_enabled"] is False


def test_backtests_are_honest_empty_state_without_fake_performance() -> None:
    body = client().get("/backtests").text
    assert "Backtesting is not implemented yet" in body
    assert "Not available" in body
    assert "fake equity curve" not in body.lower()
    assert "Phase 2 paused" in body


def test_factor_metrics_have_required_warnings_and_semantics() -> None:
    payload = client().get("/api/dashboard/factors/momentum_5").json()
    assert payload["summary"]["return_semantics"] == "gross-before-costs"
    assert "Incomplete PIT reference data" in payload["warning"]
    assert "Synthetic fixture" in payload["fixture_label"]


def test_synthetic_experiment_warning_and_promotion_disabled() -> None:
    payload = client().get("/api/dashboard/experiments/demo-exp-linear-v1").json()
    assert payload["promotion_enabled"] is False
    assert payload["holdout_access_count"] == 0
    assert "perfect metrics are expected" in payload["synthetic_warning"]
    assert payload["bootstrap_interval"] == [1.0, 1.0]
    assert set(payload["promotion_reasons"]) == {
        "Incomplete PIT data",
        "Demo fixture",
        "No real holdout validation",
        "No transaction costs",
        "No backtest",
    }


def test_cli_dashboard_defaults_to_localhost() -> None:
    with patch("uvicorn.run") as run:
        assert main(["--config", "config/default.yaml", "dashboard", "--demo"]) == 0
    assert run.call_args.kwargs["host"] == "127.0.0.1"
    assert run.call_args.kwargs["port"] == 8765


def test_cli_requires_explicit_demo_mode(capsys) -> None:
    assert main(["--config", "config/default.yaml", "dashboard"]) == 2
    assert "requires --demo" in capsys.readouterr().err
