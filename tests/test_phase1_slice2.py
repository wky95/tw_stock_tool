from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from island_quant.data.availability import AvailabilityPolicy
from island_quant.data.calendar import (
    CanonicalTradingCalendar,
    ObservationStatus,
    SessionStatus,
)
from island_quant.data.corporate_actions import (
    ActionQuality,
    CapitalReductionSubtype,
    CorporateAction,
    CorporateActionType,
    corporate_actions_frame,
    effective_actions,
)
from island_quant.data.price_views import build_price_views
from island_quant.labels.forward_returns import (
    ForwardReturnLabelBuilder,
    LabelKind,
    LabelSpec,
)
from island_quant.storage.local import LocalArtifactStore
from island_quant.storage.provenance import CodeProvenance

POLICY = AvailabilityPolicy()
INGESTED = datetime(2024, 1, 12, tzinfo=UTC)
SESSIONS = [
    date(2024, 1, 2),
    date(2024, 1, 3),
    date(2024, 1, 4),
    date(2024, 1, 5),
    date(2024, 1, 8),
    date(2024, 1, 9),
    date(2024, 1, 10),
]


def action(
    kind: CorporateActionType,
    effective: date,
    *,
    event_id: str = "event-1",
    ratio: float | None = None,
    cash: float | None = None,
    revision: int = 1,
    available: datetime | None = datetime(2024, 1, 2, 8, tzinfo=UTC),
    capital_reduction_subtype: CapitalReductionSubtype | None = None,
) -> CorporateAction:
    return CorporateAction(
        instrument_id="1111",
        action_type=kind,
        announcement_time=datetime(2024, 1, 2, 7, tzinfo=UTC),
        ex_date=effective,
        effective_date=effective,
        record_date=None,
        payment_date=None,
        ratio=ratio,
        cash_amount=cash,
        currency="TWD" if cash is not None else None,
        source="fixture",
        source_event_id=event_id,
        ingested_at=INGESTED,
        available_at=available,
        revision=revision,
        quality=ActionQuality.CONFIRMED,
        capital_reduction_subtype=(
            capital_reduction_subtype
            if kind is CorporateActionType.CAPITAL_REDUCTION
            else None
        )
        or (
            CapitalReductionSubtype.LOSS_OFFSET
            if kind is CorporateActionType.CAPITAL_REDUCTION
            else None
        ),
    )


def prices(
    closes: list[float], volumes: list[int] | None = None, instrument_id: str = "1111"
) -> pl.DataFrame:
    days = SESSIONS[: len(closes)]
    actual_volumes = volumes or [100] * len(closes)
    return pl.DataFrame(
        [
            {
                "instrument_id": instrument_id,
                "trade_date": day,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": volume,
                "volume_unit": "shares",
                "available_at": POLICY.available_at(day),
                "price_view": "canonical_unadjusted",
            }
            for day, close, volume in zip(days, closes, actual_volumes, strict=True)
        ],
        infer_schema_length=None,
    )


def view_bundle(source: pl.DataFrame, actions: list[CorporateAction]):
    action_frame = corporate_actions_frame(actions) if actions else pl.DataFrame()
    return build_price_views(
        source,
        action_frame,
        raw_dataset_version="raw-v1",
        corporate_action_version="actions-v1",
        availability_policy=POLICY,
    )


def calendar() -> CanonicalTradingCalendar:
    return CanonicalTradingCalendar.from_trading_dates(
        SESSIONS, SESSIONS[0], SESSIONS[-1], "calendar-v1"
    )


def universe(symbols: list[str] | None = None) -> pl.DataFrame:
    actual = symbols or ["1111"]
    return pl.DataFrame(
        [
            {
                "trade_date": SESSIONS[0],
                "instrument_id": symbol,
                "market": "twse",
                "eligible": True,
            }
            for symbol in actual
        ]
    )


def instruments(listing: date = SESSIONS[0], delisting: date | None = None) -> pl.DataFrame:
    return pl.DataFrame(
        [{"instrument_id": "1111", "listing_date": listing, "delisting_date": delisting}]
    )


def builder() -> ForwardReturnLabelBuilder:
    return ForwardReturnLabelBuilder(POLICY)


def test_split_adjustment_preserves_price_continuity_and_reverses_volume() -> None:
    source = prices([100, 50], [100, 200])
    bundle = view_bundle(source, [action(CorporateActionType.SPLIT, SESSIONS[1], ratio=2)])

    assert bundle.canonical_unadjusted["close"].to_list() == [100.0, 50.0]
    assert bundle.split_adjusted["close"].to_list() == [50.0, 50.0]
    assert bundle.split_adjusted["volume"].to_list() == [200, 200]
    assert bundle.adjustment_factors["split_price_factor"].to_list() == [0.5, 1.0]


@pytest.mark.parametrize(
    ("kind", "ratio"),
    [
        (CorporateActionType.REVERSE_SPLIT, 0.5),
        (CorporateActionType.CAPITAL_REDUCTION, 0.5),
    ],
)
def test_reverse_split_and_capital_reduction_math(kind: CorporateActionType, ratio: float) -> None:
    bundle = view_bundle(prices([50, 100], [200, 100]), [action(kind, SESSIONS[1], ratio=ratio)])
    assert bundle.split_adjusted["close"].to_list() == [100.0, 100.0]
    assert bundle.split_adjusted["volume"].to_list() == [100, 100]


def test_cash_dividend_total_return_continuity() -> None:
    bundle = view_bundle(
        prices([100, 95]),
        [action(CorporateActionType.CASH_DIVIDEND, SESSIONS[1], cash=5)],
    )
    assert bundle.split_adjusted["close"].to_list() == [100.0, 95.0]
    assert bundle.total_return["close"].to_list() == [95.0, 95.0]
    assert bundle.adjustment_factors["cash_total_return_factor"].to_list() == [0.95, 1.0]


def test_revision_keeps_history_and_latest_revision_wins() -> None:
    actions = corporate_actions_frame(
        [
            action(CorporateActionType.SPLIT, SESSIONS[1], ratio=2, revision=1),
            action(CorporateActionType.SPLIT, SESSIONS[1], ratio=3, revision=2),
        ]
    )
    assert actions.height == 2
    selected = effective_actions(actions)
    assert selected.height == 1
    assert selected.row(0, named=True)["ratio"] == 3


def test_announcement_and_availability_filtering_prevents_early_use() -> None:
    late = datetime(2024, 1, 6, tzinfo=UTC)
    actions = corporate_actions_frame(
        [action(CorporateActionType.SPLIT, SESSIONS[4], ratio=2, available=late)]
    )
    before = effective_actions(actions, datetime(2024, 1, 5, tzinfo=UTC))
    after = effective_actions(actions, datetime(2024, 1, 7, tzinfo=UTC))
    assert before.height == 0
    assert after.height == 1


def test_delisting_boundary_invalidates_forward_label() -> None:
    labels = builder().build(
        LabelSpec(LabelKind.NEXT_SESSION_OPEN_TO_OPEN, "label-v1"),
        prices([10, 11, 12]),
        universe(),
        calendar(),
        dataset_version="data-v1",
        price_view_version="canonical-v1",
        quality_passed=True,
        quality_report_version="quality-v1",
        instruments=instruments(delisting=SESSIONS[2]),
    )
    assert labels.row(0, named=True)["invalid_reason"] == "listing_or_delisting_boundary"


def test_listing_boundary_invalidates_forward_label() -> None:
    labels = builder().build(
        LabelSpec(LabelKind.NEXT_SESSION_OPEN_TO_OPEN, "label-v1"),
        prices([10, 11, 12]),
        universe(),
        calendar(),
        dataset_version="data-v1",
        price_view_version="canonical-v1",
        quality_passed=True,
        quality_report_version="quality-v1",
        instruments=instruments(listing=SESSIONS[2]),
    )
    assert labels.row(0, named=True)["invalid_reason"] == "listing_or_delisting_boundary"


def test_suspension_or_missing_entry_invalidates_forward_label() -> None:
    labels = builder().build(
        LabelSpec(LabelKind.NEXT_SESSION_OPEN_TO_CLOSE, "label-v1"),
        prices([10, 11], [100, 0]),
        universe(),
        calendar(),
        dataset_version="data-v1",
        price_view_version="canonical-v1",
        quality_passed=True,
        quality_report_version="quality-v1",
    )
    assert labels.row(0, named=True)["invalid_reason"] == "unexecutable_entry_observation"


def test_holiday_and_weekend_are_skipped_by_next_session() -> None:
    market_calendar = calendar()
    assert market_calendar.next_session(date(2024, 1, 5)) == date(2024, 1, 8)
    weekend = market_calendar.sessions.filter(pl.col("trade_date") == date(2024, 1, 6))
    assert weekend.row(0, named=True)["status"] == SessionStatus.HOLIDAY.value
    assert set(market_calendar.sessions["timezone"]) == {"Asia/Taipei"}


def test_suspension_is_not_misclassified_as_market_closure() -> None:
    status = calendar().classify_observation(
        SESSIONS[1], SESSIONS[0], None, has_observation=False, suspended=True
    )
    assert status is ObservationStatus.SUSPENDED


def test_zero_volume_observation_is_retained_and_classified() -> None:
    status = calendar().classify_observation(
        SESSIONS[1], SESSIONS[0], None, has_observation=True, volume=0
    )
    assert status is ObservationStatus.ZERO_VOLUME


def test_cross_sectional_rank_uses_only_decision_date_eligible_universe() -> None:
    combined = pl.concat(
        [
            prices([10, 11, 12], instrument_id="1111"),
            prices([10, 12, 15], instrument_id="2222"),
            prices([10, 20, 40], instrument_id="9999"),
        ]
    )
    members = universe(["1111", "2222"]).vstack(
        pl.DataFrame(
            [
                {
                    "trade_date": SESSIONS[0],
                    "instrument_id": "9999",
                    "market": "twse",
                    "eligible": False,
                }
            ]
        )
    )
    labels = builder().build(
        LabelSpec(LabelKind.CROSS_SECTIONAL_RANK, "rank-v1"),
        combined,
        members,
        calendar(),
        dataset_version="data-v1",
        price_view_version="canonical-v1",
        quality_passed=True,
        quality_report_version="quality-v1",
    )
    assert set(labels["instrument_id"]) == {"1111", "2222"}
    assert sorted(labels["label_value"].to_list()) == [0.0, 1.0]


def test_label_never_uses_same_day_execution_price() -> None:
    labels = builder().build(
        LabelSpec(LabelKind.NEXT_SESSION_OPEN_TO_CLOSE, "label-v1"),
        prices([10, 11]),
        universe(),
        calendar(),
        dataset_version="data-v1",
        price_view_version="canonical-v1",
        quality_passed=True,
        quality_report_version="quality-v1",
    )
    row = labels.row(0, named=True)
    assert json.loads(row["entry_observation"])["trade_date"] == "2024-01-03"
    assert row["earliest_execution_time"].date() > row["decision_date"]
    assert row["decision_time"].tzinfo is not None
    assert row["return_definition"].startswith("gross_")


def test_n_session_label_uses_sessions_not_calendar_days() -> None:
    labels = builder().build(
        LabelSpec(LabelKind.N_SESSION_OPEN_TO_OPEN, "label-v1", horizon_sessions=3),
        prices([10, 11, 12, 13, 15]),
        universe(),
        calendar(),
        dataset_version="data-v1",
        price_view_version="canonical-v1",
        quality_passed=True,
        quality_report_version="quality-v1",
    )
    row = labels.row(0, named=True)
    assert json.loads(row["entry_observation"])["trade_date"] == "2024-01-03"
    assert json.loads(row["exit_observation"])["trade_date"] == "2024-01-08"
    assert row["label_value"] == pytest.approx(15 / 11 - 1)


def test_benchmark_relative_label_uses_identical_window() -> None:
    benchmark = pl.DataFrame(
        [
            {"market": "twse", "trade_date": day, "open": value, "close": value}
            for day, value in zip(SESSIONS[:3], [99.0, 100.0, 102.0], strict=True)
        ]
    )
    labels = builder().build(
        LabelSpec(LabelKind.BENCHMARK_RELATIVE, "relative-v1"),
        prices([10, 11, 12]),
        universe(),
        calendar(),
        dataset_version="data-v1",
        price_view_version="canonical-v1",
        quality_passed=True,
        quality_report_version="quality-v1",
        benchmark_prices=benchmark,
    )
    row = labels.row(0, named=True)
    assert row["benchmark_return"] == pytest.approx(0.02)
    assert row["label_value"] == pytest.approx((12 / 11 - 1) - 0.02)


def test_quality_gate_and_finalization_buffer_control_decision_snapshot() -> None:
    source = prices([10])
    with pytest.raises(RuntimeError, match="quality checks"):
        POLICY.decision_snapshot(source, POLICY.decision_time(SESSIONS[0]), quality_passed=False)
    accepted = POLICY.decision_snapshot(
        source, POLICY.decision_time(SESSIONS[0]), quality_passed=True
    )
    assert accepted.height == 1
    with pytest.raises(RuntimeError, match="quality report"):
        builder().build(
            LabelSpec(LabelKind.NEXT_SESSION_OPEN_TO_CLOSE, "label-v1"),
            prices([10, 11]),
            universe(),
            calendar(),
            dataset_version="data-v1",
            price_view_version="canonical-v1",
            quality_passed=False,
            quality_report_version="quality-v1",
        )


def test_illegal_corporate_action_fails_closed() -> None:
    with pytest.raises(ValueError, match="ratio must be positive"):
        action(CorporateActionType.SPLIT, SESSIONS[1], ratio=0)


def test_analytical_and_point_in_time_adjustment_views_are_distinct() -> None:
    future = action(
        CorporateActionType.SPLIT,
        SESSIONS[1],
        ratio=2,
        available=datetime(2024, 1, 4, tzinfo=UTC),
    )
    source = prices([100, 50])
    analytical = view_bundle(source, [future])
    point_in_time = build_price_views(
        source,
        corporate_actions_frame([future]),
        raw_dataset_version="raw-v1",
        corporate_action_version="actions-v1",
        availability_policy=POLICY,
        decision_time=datetime(2024, 1, 3, tzinfo=UTC),
    )
    assert analytical.split_adjusted["close"].to_list() == [50.0, 50.0]
    assert set(analytical.split_adjusted["uses_future_actions"]) == {True}
    assert point_in_time.split_adjusted["close"].to_list() == [100.0, 50.0]
    assert set(point_in_time.split_adjusted["uses_future_actions"]) == {False}


def make_store(root: Path) -> LocalArtifactStore:
    return LocalArtifactStore(
        root / "raw", root / "normalized", root / "checkpoint", root / "catalog.duckdb"
    )


def make_store_with_provenance(
    root: Path, provenance: CodeProvenance
) -> LocalArtifactStore:
    return LocalArtifactStore(
        root / "raw",
        root / "normalized",
        root / "checkpoint",
        root / "catalog.duckdb",
        provenance_provider=lambda: provenance,
    )


def metadata() -> dict[str, object]:
    return {
        "schema_version": 1,
        "created_at": "2024-01-01T00:00:00+00:00",
        "source_checksums": ["source-v1"],
        "transformation_code_version": "code-v1",
        "configuration_version": "config-v1",
    }


def test_fixed_dataset_version_remains_reproducible_after_new_candidate(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    old = store.write_dataset("prices", pl.DataFrame({"x": [1]}), metadata())
    store.stage_dataset("prices", pl.DataFrame({"x": [2]}), metadata())
    assert store.read_dataset_version("prices", old.checksum).to_dicts() == [{"x": 1}]
    assert store.current_version("prices") == old.checksum

    revised_metadata = {**metadata(), "configuration_version": "config-v2"}
    revised = store.stage_dataset("prices", pl.DataFrame({"x": [1]}), revised_metadata)
    assert revised.checksum != old.checksum


def test_candidate_promotion_atomically_changes_current_pointer(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.write_dataset("prices", pl.DataFrame({"x": [1]}), metadata())
    candidate = store.stage_dataset("prices", pl.DataFrame({"x": [2]}), metadata())
    assert store.current_version("prices") == first.checksum
    store.promote_dataset("prices", candidate.checksum)
    assert store.current_version("prices") == candidate.checksum
    assert store.read_latest_dataset("prices").to_dicts() == [{"x": 2}]


def test_failed_candidate_promotion_keeps_previous_current_pointer(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    current = store.write_dataset("prices", pl.DataFrame({"x": [1]}), metadata())
    candidate = store.stage_dataset("prices", pl.DataFrame({"x": [2]}), metadata())
    candidate.parquet_path.write_bytes(b"corrupted candidate")
    with pytest.raises((OSError, RuntimeError, pl.exceptions.ComputeError)):
        store.promote_dataset("prices", candidate.checksum)
    assert store.current_version("prices") == current.checksum


def test_dirty_source_tree_changes_version_and_cannot_be_validated(tmp_path: Path) -> None:
    first_store = make_store_with_provenance(
        tmp_path / "first", CodeProvenance("commit-a", True, "tree-a")
    )
    second_store = make_store_with_provenance(
        tmp_path / "second", CodeProvenance("commit-a", True, "tree-b")
    )
    first = first_store.stage_dataset("prices", pl.DataFrame({"x": [1]}), metadata())
    second = second_store.stage_dataset("prices", pl.DataFrame({"x": [1]}), metadata())
    assert first.checksum != second.checksum
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["git_commit"] == "commit-a"
    assert manifest["dirty"] is True
    assert manifest["source_tree_hash"] == "tree-a"
    assert manifest["configuration_hash"]
    with pytest.raises(RuntimeError, match="rejects dirty"):
        first_store.promote_dataset("prices", first.checksum, validated=True)


def test_concurrent_promotion_uses_compare_and_swap(tmp_path: Path) -> None:
    clean = CodeProvenance("commit-a", False, "tree-a")
    store = make_store_with_provenance(tmp_path, clean)
    current = store.write_dataset("prices", pl.DataFrame({"x": [1]}), metadata())
    candidates = [
        store.stage_dataset("prices", pl.DataFrame({"x": [value]}), metadata())
        for value in (2, 3)
    ]

    def promote(version: str) -> str:
        try:
            store.promote_dataset(
                "prices", version, expected_current_version=current.checksum
            )
            return "promoted"
        except RuntimeError:
            return "cas_rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(promote, [item.checksum for item in candidates]))
    assert sorted(results) == ["cas_rejected", "promoted"]
    pointer = json.loads(
        (tmp_path / "normalized" / "prices" / "current.json").read_text(encoding="utf-8")
    )
    assert pointer["checksum"] in {item.checksum for item in candidates}
    assert pointer["generation"] == 2


def test_unsupported_and_ambiguous_corporate_actions_fail_closed() -> None:
    source = prices([100, 90])
    rights = action(CorporateActionType.RIGHTS_ISSUE, SESSIONS[1], ratio=1.1)
    with pytest.raises(ValueError, match="UNSUPPORTED_CORPORATE_ACTION_ADJUSTMENT"):
        view_bundle(source, [rights])

    cash_reduction = action(
        CorporateActionType.CAPITAL_REDUCTION,
        SESSIONS[1],
        ratio=0.9,
        capital_reduction_subtype=CapitalReductionSubtype.CASH,
    )
    with pytest.raises(ValueError, match="UNSUPPORTED_CORPORATE_ACTION_ADJUSTMENT"):
        view_bundle(source, [cash_reduction])

    split = action(CorporateActionType.SPLIT, SESSIONS[1], event_id="split", ratio=2)
    dividend = action(
        CorporateActionType.CASH_DIVIDEND, SESSIONS[1], event_id="dividend", cash=5
    )
    with pytest.raises(ValueError, match="UNSUPPORTED_SAME_DAY_CORPORATE_ACTIONS"):
        view_bundle(source, [dividend, split])
