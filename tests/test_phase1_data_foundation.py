from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import polars as pl
import pytest
import yaml

from island_quant.cli import main
from island_quant.data.adapters.fixture import FixtureProvider
from island_quant.data.ingestion import IngestionService
from island_quant.data.schema import normalize_calendar, normalize_instruments, normalize_prices
from island_quant.data.universe import UniversePolicy, require_research_complete
from island_quant.data.validation import validate_daily_prices
from island_quant.storage.local import LocalArtifactStore

FIXTURES = Path(__file__).parent / "fixtures" / "phase1"
SYMBOLS = ["1111", "2222", "3333"]


def make_store(root: Path) -> LocalArtifactStore:
    return LocalArtifactStore(
        root / "raw",
        root / "normalized",
        root / "checkpoints",
        root / "catalog.duckdb",
    )


def make_policy() -> UniversePolicy:
    return UniversePolicy(
        version="test-v1",
        minimum_listing_days=1,
        trailing_median_window=2,
        minimum_trailing_median_traded_value=Decimal("1000"),
        minimum_lookback_observations=2,
    )


def run_fixture(root: Path, provider: FixtureProvider | None = None):
    actual_provider = provider or FixtureProvider(FIXTURES)
    service = IngestionService(actual_provider, make_store(root), make_policy())
    result = service.run("2024-01-02", "2024-01-10", SYMBOLS)
    return actual_provider, result


def test_ingestion_is_idempotent_and_resumes_from_raw_checkpoint(tmp_path: Path) -> None:
    provider, first = run_fixture(tmp_path)
    first_fetch_count = provider.fetch_count
    _, second = run_fixture(tmp_path, provider)

    assert first_fetch_count == 6
    assert provider.fetch_count == first_fetch_count
    assert first.job_id == second.job_id
    assert {name: item.checksum for name, item in first.artifacts.items()} == {
        name: item.checksum for name, item in second.artifacts.items()
    }
    prices = make_store(tmp_path).read_latest_dataset("daily_prices")
    assert prices is not None
    assert prices.height == 13
    assert prices.select("instrument_id", "trade_date").is_duplicated().sum() == 0


def test_fixed_input_produces_fixed_checksums_across_stores(tmp_path: Path) -> None:
    _, first = run_fixture(tmp_path / "first")
    _, second = run_fixture(tmp_path / "second")

    assert {name: item.checksum for name, item in first.artifacts.items()} == {
        name: item.checksum for name, item in second.artifacts.items()
    }


def test_point_in_time_universe_preserves_delisted_history(tmp_path: Path) -> None:
    run_fixture(tmp_path)
    store = make_store(tmp_path)
    instruments = store.read_latest_dataset("instrument_master")
    prices = store.read_latest_dataset("daily_prices")
    universe = store.read_latest_dataset("universe_membership")
    metadata = store.read_latest_dataset("universe_metadata")
    assert instruments is not None and prices is not None and universe is not None
    assert metadata is not None

    delisted = instruments.filter(pl.col("instrument_id") == "2222").row(0, named=True)
    assert str(delisted["delisting_date"]) == "2024-01-08"
    assert delisted["listing_date_source"] == "first_observed_price"
    assert delisted["listing_date_quality"] == "provisional"
    assert delisted["observed_at"].tzinfo is not None
    assert prices.filter(pl.col("instrument_id") == "2222").height == 4

    before_listing = universe.filter(
        (pl.col("instrument_id") == "3333")
        & (pl.col("trade_date") == pl.date(2024, 1, 5))
    ).row(0, named=True)
    after_delisting = universe.filter(
        (pl.col("instrument_id") == "2222")
        & (pl.col("trade_date") == pl.date(2024, 1, 8))
    ).row(0, named=True)
    eligible = universe.filter(
        (pl.col("instrument_id") == "1111")
        & (pl.col("trade_date") == pl.date(2024, 1, 4))
    ).row(0, named=True)
    assert "not_yet_listed" in before_listing["exclusion_reasons"]
    assert "delisted" in after_delisting["exclusion_reasons"]
    assert eligible["eligible"] is True
    meta = metadata.row(0, named=True)
    assert meta["universe_policy_version"] == "test-v1"
    assert meta["reference_dataset_version"]
    assert meta["known_instrument_count"] == 3
    assert meta["provisional_listing_date_count"] == 3
    assert meta["is_research_complete"] is False
    with pytest.raises(RuntimeError, match="INCOMPLETE POINT-IN-TIME"):
        require_research_complete(metadata)
    require_research_complete(metadata, allow_incomplete=True)


def test_quality_report_timezone_raw_immutability_and_catalog(tmp_path: Path) -> None:
    _, result = run_fixture(tmp_path)
    assert result.quality_report.error_count == 0
    assert any(
        issue.code == "MISSING_TRADING_DAY" and "1111:2024-01-05" in issue.primary_key
        for issue in result.quality_report.issues
    )
    prices = make_store(tmp_path).read_latest_dataset("daily_prices")
    assert prices is not None
    assert prices.schema["event_time"].time_zone == "Asia/Taipei"
    assert prices.schema["available_at"].time_zone == "Asia/Taipei"
    assert prices.schema["ingested_at"].time_zone == "UTC"
    assert set(prices.get_column("volume_unit")) == {"shares"}
    assert set(prices.get_column("source_volume_unit")) == {"shares"}

    raw_files = list((tmp_path / "raw").rglob("*.json"))
    body_files = [path for path in raw_files if ".manifest." not in path.name]
    assert body_files
    assert all(
        json.loads(path.read_text(encoding="utf-8"))["data"] is not None
        for path in body_files
    )
    assert all(str(path).startswith(str(tmp_path / "raw")) for path in body_files)
    assert all(
        str(item.parquet_path).startswith(str(tmp_path / "normalized"))
        for item in result.artifacts.values()
    )

    with duckdb.connect(str(tmp_path / "catalog.duckdb"), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM daily_prices").fetchone()[0] == 13


def test_validator_detects_duplicate_negative_and_invalid_ohlc() -> None:
    fetched_at = datetime(2024, 1, 11, tzinfo=UTC)
    valid = {
        "date": "2024-01-02",
        "stock_id": "1111",
        "Trading_Volume": 10,
        "Trading_money": 1000,
        "open": 10,
        "max": 11,
        "min": 9,
        "close": 10,
        "Trading_turnover": 2,
    }
    negative = {**valid, "date": "2024-01-03", "open": -1}
    invalid = {**valid, "date": "2024-01-04", "max": 8}
    negative_volume = {**valid, "date": "2024-01-05", "Trading_Volume": -1}
    prices = normalize_prices(
        [valid, valid, negative, invalid, negative_volume], "fixture", fetched_at, 1
    )
    calendar = normalize_calendar(
        [
            {"date": "2024-01-02"},
            {"date": "2024-01-03"},
            {"date": "2024-01-04"},
            {"date": "2024-01-05"},
        ],
        "fixture",
        fetched_at,
        1,
    )
    instruments = normalize_instruments(
        [
            {
                "date": "2024-01-04",
                "stock_id": "1111",
                "stock_name": "甲",
                "type": "twse",
                "industry_category": "半導體業",
            }
        ],
        [],
        prices,
        "fixture",
        fetched_at,
        1,
    )
    report = validate_daily_prices(prices, instruments, calendar)
    codes = {issue.code for issue in report.issues}
    assert {
        "DUPLICATE_PRIMARY_KEY",
        "NEGATIVE_PRICE",
        "NEGATIVE_ACTIVITY",
        "INVALID_OHLC",
    } <= codes


def test_transfer_from_emerging_does_not_backfill_listed_market() -> None:
    fetched_at = datetime(2024, 1, 11, tzinfo=UTC)
    source_prices = [
        {
            "date": day,
            "stock_id": "5555",
            "Trading_Volume": 10,
            "Trading_money": 1000,
            "open": 10,
            "max": 11,
            "min": 9,
            "close": 10,
            "Trading_turnover": 2,
        }
        for day in ("2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08")
    ]
    prices = normalize_prices(source_prices, "fixture", fetched_at, 1)
    instruments = normalize_instruments(
        [
            {
                "date": "2024-01-04",
                "stock_id": "5555",
                "stock_name": "轉板公司",
                "type": "emerging",
                "industry_category": "其他",
            },
            {
                "date": "2024-01-10",
                "stock_id": "5555",
                "stock_name": "轉板公司",
                "type": "twse",
                "industry_category": "其他",
            },
        ],
        [],
        prices,
        "fixture",
        fetched_at,
        1,
    )
    assert str(instruments.row(0, named=True)["listing_date"]) == "2024-01-05"


def test_offline_cli_runs_complete_slice_without_network(tmp_path: Path) -> None:
    config = yaml.safe_load(Path("config/default.yaml").read_text(encoding="utf-8"))
    config["data"].update(
        {
            "provider": "fixture",
            "raw_root": str(tmp_path / "raw"),
            "normalized_root": str(tmp_path / "normalized"),
            "checkpoint_root": str(tmp_path / "checkpoints"),
            "catalog_path": str(tmp_path / "catalog.duckdb"),
        }
    )
    config["universe"].update(
        {
            "policy_version": "test-v1",
            "minimum_listing_days": 1,
            "trailing_median_window": 2,
            "minimum_trailing_median_traded_value": "1000",
            "minimum_lookback_observations": 2,
        }
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    exit_code = main(
        [
            "--config",
            str(config_path),
            "ingest-data",
            "--provider",
            "fixture",
            "--fixture-dir",
            str(FIXTURES),
            "--start",
            "2024-01-02",
            "--end",
            "2024-01-10",
            "--symbols",
            ",".join(SYMBOLS),
        ]
    )
    assert exit_code == 0
    assert (tmp_path / "catalog.duckdb").exists()
    assert main(["--config", str(config_path), "validate-data"]) == 6
    assert (
        main(["--config", str(config_path), "validate-data", "--research-override"])
        == 0
    )
