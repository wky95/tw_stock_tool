from __future__ import annotations

import hashlib
import json
from argparse import Namespace
from datetime import date, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from island_quant.config import AppSettings, PaperSettings, ResearchSettings
from island_quant.operations.cli import run_soak
from island_quant.operations.soak import PaperSoakPlan, PaperSoakRunner
from island_quant.operations.strategy import REQUIRED_LINEAGE
from island_quant.pipeline.artifacts import ExactArtifactStore

TAIPEI = ZoneInfo("Asia/Taipei")
SESSIONS = (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        research=ResearchSettings(artifact_root=tmp_path / "artifacts"),
        paper=PaperSettings(
            oms_database_path=tmp_path / "state" / "oms.sqlite",
            operations_database_path=tmp_path / "state" / "operations.sqlite",
            scheduler_database_path=tmp_path / "state" / "scheduler.sqlite",
            soak_oms_database_path=tmp_path / "soak" / "oms.sqlite",
            soak_operations_database_path=tmp_path / "soak" / "operations.sqlite",
            soak_scheduler_database_path=tmp_path / "soak" / "scheduler.sqlite",
        ),
    )


def target(root: Path) -> str:
    manifest = ExactArtifactStore(root, "paper_target_snapshots", 1).publish(
        [
            {
                "instrument_id": "2330",
                "market": "TWSE",
                "execution_session": "2025-01-02",
                "decision_time": "2025-01-01T18:00:00+08:00",
                "available_at": "2025-01-01T17:30:00+08:00",
                "target_weight": "0.10",
                "reference_price": "10",
                "eligible": True,
            }
        ],
        lineage={name: digest(name) for name in REQUIRED_LINEAGE},
        completeness="validated",
        classification="paper_candidate",
        created_at=datetime(2025, 1, 1, 19, tzinfo=TAIPEI),
    )
    return manifest.artifact_version


def plan_payload(target_version: str | None, *, stale: bool = False) -> dict[str, object]:
    prices = ("10", "11", "12")
    cycles: list[dict[str, object]] = []
    for index, session in enumerate(SESSIONS):
        as_of = datetime(session.year, session.month, session.day, 9, tzinfo=TAIPEI)
        event_time = (
            datetime(session.year, session.month, session.day, 6, tzinfo=TAIPEI)
            if stale and index == 0
            else as_of
        )
        cycles.append(
            {
                "session": session.isoformat(),
                "as_of": as_of.isoformat(),
                "market": [
                    {
                        "instrument_id": "2330",
                        "event_time": event_time.isoformat(),
                        "reference_price": prices[index],
                        "volume": 1000000,
                        "suspended": False,
                        "limit_locked": False,
                    }
                ],
                "target_artifact_version": target_version if index == 0 else None,
                "broker_participation_cap": "0.10",
            }
        )
    return {
        "schema_version": 1,
        "sessions": [item.isoformat() for item in SESSIONS],
        "instruments": [
            {
                "instrument_id": "2330",
                "market": "TWSE",
                "reference_version": digest("instrument-reference"),
            }
        ],
        "cycles": cycles,
        "require_final_settlement": True,
        "verify_duplicate_sessions": True,
    }


def write_plan(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "soak-plan.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_three_session_restart_duplicate_replay_and_t_plus_two_settlement(
    tmp_path: Path,
) -> None:
    app_settings = settings(tmp_path)
    version = target(app_settings.research.artifact_root)
    plan = PaperSoakPlan.read(write_plan(tmp_path, plan_payload(version)))
    runner = PaperSoakRunner(app_settings)
    result = runner.run(plan, dry_run=False)
    assert result.status == "passed"
    assert len(result.cycles) == 3
    assert all(item.jobs_completed == 10 for item in result.cycles)
    assert all(item.duplicate_replay_clean for item in result.cycles)
    assert result.cycles[-1].order_count == 1
    assert result.cycles[-1].fill_count == 1
    assert dict(result.checks)["final_settlement_complete"] == "passed"
    assert dict(result.checks)["all_orders_terminal"] == "passed"
    assert dict(result.checks)["reservations_released"] == "passed"
    assert result.soak_report_version is not None
    manifest = ExactArtifactStore(
        app_settings.research.artifact_root, "paper_soak_reports", 1
    ).manifest(result.soak_report_version)
    assert manifest.classification == "paper_engineering_soak"
    assert manifest.completeness == "validated"
    assert runner.run(plan, dry_run=False) == result


def test_dry_run_validates_targets_without_runtime_writes(tmp_path: Path) -> None:
    app_settings = settings(tmp_path)
    version = target(app_settings.research.artifact_root)
    plan = PaperSoakPlan.read(write_plan(tmp_path, plan_payload(version)))
    result = PaperSoakRunner(app_settings).run(plan, dry_run=True)
    assert result.status == "validated" and result.dry_run
    assert not app_settings.paper.soak_oms_database_path.exists()
    assert not (app_settings.research.artifact_root / "paper_soak_reports").exists()


def test_stale_market_drill_fails_closed_and_publishes_incomplete_report(
    tmp_path: Path,
) -> None:
    app_settings = settings(tmp_path)
    plan = PaperSoakPlan.read(write_plan(tmp_path, plan_payload(None, stale=True)))
    result = PaperSoakRunner(app_settings).run(plan, dry_run=False)
    assert result.status == "failed"
    assert result.cycles[0].safe_mode
    assert dict(result.checks)["runtime_safe_mode"] == "failed"
    assert result.soak_report_version is not None
    manifest = ExactArtifactStore(
        app_settings.research.artifact_root, "paper_soak_reports", 1
    ).manifest(result.soak_report_version)
    assert manifest.completeness == "incomplete"


def test_missing_settlement_sessions_fail_closed_without_inventing_dates(
    tmp_path: Path,
) -> None:
    app_settings = settings(tmp_path)
    version = target(app_settings.research.artifact_root)
    payload = plan_payload(version)
    payload["sessions"] = [SESSIONS[0].isoformat()]
    payload["cycles"] = cast(list[dict[str, object]], payload["cycles"])[:1]
    plan = PaperSoakPlan.read(write_plan(tmp_path, payload))
    result = PaperSoakRunner(app_settings).run(plan, dry_run=False)
    assert result.status == "failed"
    assert result.cycles[0].safe_mode
    assert dict(result.checks)["final_settlement_complete"] == "failed"


def test_partial_fill_then_process_restart_detects_missing_broker_state(
    tmp_path: Path,
) -> None:
    app_settings = settings(tmp_path)
    version = target(app_settings.research.artifact_root)
    payload = plan_payload(version)
    cycles = cast(list[dict[str, object]], payload["cycles"])
    cycles[0]["broker_participation_cap"] = "0.005"
    result = PaperSoakRunner(app_settings).run(
        PaperSoakPlan.read(write_plan(tmp_path, payload)), dry_run=False
    )
    assert result.status == "failed"
    assert result.cycles[0].fill_count == 1
    assert result.cycles[1].safe_mode
    assert dict(result.checks)["all_orders_terminal"] == "failed"
    assert dict(result.checks)["reservations_released"] == "failed"


def test_plan_schema_calendar_and_duplicate_cycles_fail_before_writes(tmp_path: Path) -> None:
    payload = plan_payload(None)
    cycles = cast(list[dict[str, object]], payload["cycles"])
    cycles.append(dict(cycles[-1]))
    with pytest.raises(ValueError, match="unique and sorted"):
        PaperSoakPlan.read(write_plan(tmp_path, payload))
    assert not (tmp_path / "state").exists()


def test_plan_rejects_string_flags_and_incomplete_market_coverage(tmp_path: Path) -> None:
    payload = plan_payload(None)
    payload["verify_duplicate_sessions"] = "false"
    with pytest.raises(ValueError, match="field types"):
        PaperSoakPlan.read(write_plan(tmp_path, payload))
    payload = plan_payload(None)
    instruments = cast(list[dict[str, object]], payload["instruments"])
    instruments.append(
        {
            "instrument_id": "2317",
            "market": "TWSE",
            "reference_version": digest("2317-reference"),
        }
    )
    with pytest.raises(ValueError, match="coverage is incomplete"):
        PaperSoakPlan.read(write_plan(tmp_path, payload))


@pytest.mark.parametrize("cap", ["NaN", "0", "1.01", True])
def test_plan_rejects_invalid_broker_failure_injection(
    tmp_path: Path, cap: object
) -> None:
    payload = plan_payload(None)
    cycles = cast(list[dict[str, object]], payload["cycles"])
    cycles[0]["broker_participation_cap"] = cap
    with pytest.raises(ValueError, match="broker participation cap"):
        PaperSoakPlan.read(write_plan(tmp_path, payload))


def test_plan_rejects_non_array_market_and_non_finite_price(tmp_path: Path) -> None:
    payload = plan_payload(None)
    cycles = cast(list[dict[str, object]], payload["cycles"])
    cycles[0]["market"] = {}
    with pytest.raises(ValueError, match="market events must be an array"):
        PaperSoakPlan.read(write_plan(tmp_path, payload))
    payload = plan_payload(None)
    cycles = cast(list[dict[str, object]], payload["cycles"])
    market = cast(list[dict[str, object]], cycles[0]["market"])
    market[0]["reference_price"] = "Infinity"
    with pytest.raises(ValueError, match="reference price must be finite"):
        PaperSoakPlan.read(write_plan(tmp_path, payload))


def test_cli_requires_confirmation_and_supports_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app_settings = settings(tmp_path)
    version = target(app_settings.research.artifact_root)
    path = write_plan(tmp_path, plan_payload(version))
    assert run_soak(Namespace(plan=path, dry_run=False, confirm=False), app_settings) == 2
    assert "--dry-run or --confirm" in capsys.readouterr().err
    assert run_soak(Namespace(plan=path, dry_run=True, confirm=False), app_settings) == 0
    assert '"status": "validated"' in capsys.readouterr().out


def test_soak_state_paths_must_be_distinct_from_operational_state(tmp_path: Path) -> None:
    shared = tmp_path / "shared.sqlite"
    with pytest.raises(ValueError, match="must use distinct paths"):
        PaperSettings(oms_database_path=shared, soak_oms_database_path=shared)
