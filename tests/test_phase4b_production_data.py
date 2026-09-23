from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from island_quant.data.production import (
    BenchmarkConstituentRecord,
    CalendarStatus,
    ConformanceCode,
    CorporateActionRevision,
    CoverageReport,
    CoverageRequirement,
    EntitlementReference,
    OfficialCalendarRecord,
    PITInstrumentRecord,
    ProviderValue,
    RetentionLicensePolicy,
    StreamHealthState,
    StreamingObservation,
    TradabilityRecord,
    TradabilityStatus,
    assess_stream,
    effective_corporate_actions,
    evaluate_offline_conformance,
    reconcile_provider_values,
)

NOW = datetime(2025, 1, 2, 9, tzinfo=UTC)
DAY = date(2025, 1, 2)
ENTITLEMENT = EntitlementReference("fixture-entitlement-ref", "offline-fixture")


def observation(sequence: int, *, event_id: str | None = None) -> StreamingObservation:
    return StreamingObservation(
        "fixture-stream",
        event_id or f"event-{sequence}",
        sequence,
        NOW,
        NOW,
        f"checksum-{sequence}",
    )


def inputs() -> dict[str, object]:
    requirement = CoverageRequirement(
        "fixture-dataset",
        frozenset({"event_time", "available_at"}),
        timedelta(seconds=2),
        date(2020, 1, 1),
        Decimal("1"),
        ENTITLEMENT,
    )
    return {
        "decision_time": NOW,
        "instrument": PITInstrumentRecord(
            "instrument-1",
            "AAA",
            "fixture-symbol",
            "fixture-market",
            DAY,
            None,
            NOW - timedelta(days=1),
            NOW - timedelta(days=1),
            "instrument-r1",
        ),
        "session_date": DAY,
        "corporate_actions": (),
        "tradability": TradabilityRecord(
            "instrument-1",
            DAY,
            TradabilityStatus.TRADABLE,
            NOW - timedelta(days=1),
            NOW - timedelta(days=1),
        ),
        "calendar": OfficialCalendarRecord(
            "fixture-market",
            DAY,
            CalendarStatus.OPEN,
            NOW - timedelta(days=30),
            NOW - timedelta(days=30),
            "calendar-r1",
        ),
        "benchmark_required": True,
        "benchmark": BenchmarkConstituentRecord(
            "fixture-index",
            "instrument-1",
            DAY,
            None,
            Decimal("1"),
            Decimal("100"),
            Decimal("1000"),
            NOW - timedelta(days=1),
            "benchmark-r1",
        ),
        "stream_health": assess_stream(
            (observation(1),), observed_at=NOW, maximum_age=timedelta(seconds=2)
        ),
        "license_policy": RetentionLicensePolicy(
            "fixture-license-v1",
            ENTITLEMENT,
            NOW - timedelta(days=30),
            NOW + timedelta(days=30),
            timedelta(days=365),
            True,
            True,
            True,
        ),
        "oldest_retained_at": NOW - timedelta(days=30),
        "reconciliation": reconcile_provider_values(
            (
                ProviderValue("fixture-a", "record-1", "same"),
                ProviderValue("fixture-b", "record-1", "same"),
            )
        ),
        "coverage": CoverageReport(
            "fixture-dataset", 10, 10, frozenset(), timedelta(seconds=1), True, True
        ),
        "coverage_requirement": requirement,
    }


def violation(**changes: object) -> frozenset[ConformanceCode]:
    values = inputs()
    values.update(changes)
    return evaluate_offline_conformance(**values).violations  # type: ignore[arg-type]


def test_complete_fixture_snapshot_passes_without_fallback() -> None:
    result = evaluate_offline_conformance(**inputs())  # type: ignore[arg-type]
    assert result.passed and not result.violations and not result.silent_fallback_used


def test_late_announcement_fails_closed() -> None:
    action = CorporateActionRevision(
        "action-1", "instrument-1", "split", DAY, NOW, NOW + timedelta(minutes=1), 1
    )
    assert ConformanceCode.LATE_ANNOUNCEMENT in violation(corporate_actions=(action,))


def test_same_day_corporate_action_correction_selects_latest_visible_revision() -> None:
    first = CorporateActionRevision(
        "action-1", "instrument-1", "cash", DAY, NOW, NOW, 1
    )
    corrected = replace(
        first,
        revision=2,
        supersedes_revision=1,
        available_at=NOW + timedelta(hours=1),
    )
    assert effective_corporate_actions((first, corrected), NOW) == (first,)
    assert effective_corporate_actions((first, corrected), NOW + timedelta(hours=1)) == (corrected,)


@pytest.mark.parametrize(
    "instrument",
    [
        PITInstrumentRecord(
            "instrument-1",
            "AAA",
            "fixture-symbol",
            "fixture-market",
            date(2025, 1, 3),
            None,
            NOW,
            NOW,
            "future-listing",
        ),
        PITInstrumentRecord(
            "instrument-1",
            "AAA",
            "fixture-symbol",
            "fixture-market",
            date(2024, 1, 1),
            date(2025, 1, 1),
            NOW - timedelta(days=365),
            NOW - timedelta(days=365),
            "delisted",
        ),
    ],
)
def test_listing_and_delisting_boundaries_fail_closed(instrument: PITInstrumentRecord) -> None:
    assert ConformanceCode.INSTRUMENT_OUTSIDE_VALIDITY in violation(instrument=instrument)


def test_identifier_change_keeps_stable_instrument_identity() -> None:
    before = inputs()["instrument"]
    assert isinstance(before, PITInstrumentRecord)
    after = replace(before, identifier="BBB", valid_from=date(2025, 1, 3), revision_id="r2")
    assert after.instrument_id == before.instrument_id and after.identifier != before.identifier


@pytest.mark.parametrize(
    "status",
    [TradabilityStatus.SUSPENDED, TradabilityStatus.RESUMED],
)
def test_suspension_and_resumption_require_explicit_tradable_state(
    status: TradabilityStatus,
) -> None:
    tradability = replace(inputs()["tradability"], status=status)  # type: ignore[arg-type]
    assert ConformanceCode.NOT_TRADABLE in violation(tradability=tradability)


def test_price_limit_exception_without_code_fails_closed() -> None:
    tradability = replace(
        inputs()["tradability"],  # type: ignore[arg-type]
        status=TradabilityStatus.PRICE_LIMITED,
        exception_code=None,
    )
    codes = violation(tradability=tradability)
    assert ConformanceCode.PRICE_LIMIT_EXCEPTION_UNKNOWN in codes


def test_unexpected_closure_fails_closed() -> None:
    calendar = replace(
        inputs()["calendar"], status=CalendarStatus.UNEXPECTED_CLOSURE  # type: ignore[arg-type]
    )
    assert ConformanceCode.MARKET_CLOSED in violation(calendar=calendar)


def test_missing_benchmark_constituent_fails_closed() -> None:
    assert ConformanceCode.BENCHMARK_CONSTITUENT_MISSING in violation(benchmark=None)


@pytest.mark.parametrize(
    ("observations", "expected"),
    [
        ((observation(1), observation(3)), StreamHealthState.GAP),
        ((observation(1), observation(2, event_id="event-1")), StreamHealthState.DUPLICATE),
        ((observation(2), observation(1)), StreamHealthState.OUT_OF_ORDER),
    ],
)
def test_sequence_gap_duplicate_and_out_of_order_are_distinct(
    observations: tuple[StreamingObservation, ...], expected: StreamHealthState
) -> None:
    health = assess_stream(observations, observed_at=NOW, maximum_age=timedelta(seconds=2))
    assert health.state is expected


def test_stale_realtime_event_fails_closed() -> None:
    stale = assess_stream(
        (observation(1),), observed_at=NOW + timedelta(seconds=3), maximum_age=timedelta(seconds=2)
    )
    assert ConformanceCode.STREAM_STALE in violation(stream_health=stale)


def test_entitlement_expiry_fails_closed() -> None:
    policy = replace(inputs()["license_policy"], valid_until=NOW - timedelta(seconds=1))  # type: ignore[arg-type]
    assert ConformanceCode.ENTITLEMENT_EXPIRED in violation(license_policy=policy)


def test_retention_policy_violation_fails_closed() -> None:
    assert ConformanceCode.RETENTION_VIOLATION in violation(
        oldest_retained_at=NOW - timedelta(days=366)
    )


def test_provider_disagreement_requires_operator_decision() -> None:
    report = reconcile_provider_values(
        (
            ProviderValue("fixture-a", "record-1", "a"),
            ProviderValue("fixture-b", "record-1", "b"),
        )
    )
    assert report.operator_decision_required and report.selected_provider is None
    assert ConformanceCode.PROVIDER_DISAGREEMENT in violation(reconciliation=report)


def test_silent_fallback_is_prohibited() -> None:
    assert ConformanceCode.SILENT_FALLBACK in violation(fallback_provider="fixture-fallback")


def test_incomplete_pit_coverage_fails_closed() -> None:
    coverage = replace(inputs()["coverage"], observed_records=9)  # type: ignore[arg-type]
    assert ConformanceCode.INCOMPLETE_PIT_COVERAGE in violation(coverage=coverage)


def test_machine_readable_decision_pack_is_closed_and_complete() -> None:
    root = Path("docs/readiness")
    readiness = json.loads((root / "phase4b_production_readiness.json").read_text())
    requirements = json.loads((root / "production_data_requirements.json").read_text())
    evidence = json.loads((root / "shioaji_semantic_evidence.json").read_text())
    questionnaire = json.loads((root / "license_questionnaire.json").read_text())
    provider = json.loads((root / "provider_evaluation_template.json").read_text())
    transition = json.loads((root / "production_data_transition_plan.json").read_text())
    assert readiness["live_trading_ready"] is False
    assert readiness["production_data_adapter_implemented"] is False
    assert requirements["silent_fallback_permitted"] is False
    assert requirements["acceptance_thresholds"]["critical_dataset_record_coverage"] == "1.000000"
    assert len(evidence["items"]) == 8
    assert all(
        {"query_date", "url", "version", "evidence", "conclusion", "unknown"} <= set(item)
        for item in evidence["items"]
    )
    assert all(question["answer"] == "unknown" for question in questionnaire["questions"])
    assert provider["provider_name"] == "UNKNOWN" and provider["template"] is True
    assert transition["parallel_run"]["acceptance"]["silent_fallbacks"] == 0
    assert all(
        row["retention"] == "UNKNOWN"
        for row in transition["retention_deletion_backup_matrix"]
    )
