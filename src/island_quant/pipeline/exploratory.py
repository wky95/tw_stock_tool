"""Bounded real-cache exploratory pipeline without fixture fallback."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from island_quant.analytics.performance import PerformanceAnalyzer, PerformancePolicy, attribute
from island_quant.backtest.artifacts import BacktestArtifactStore
from island_quant.backtest.contracts import BacktestSession, PredictionObservation
from island_quant.backtest.engine import BacktestConfig, EventDrivenBacktestEngine
from island_quant.backtest.policies import FeeTaxPolicy, FillPolicy, RiskPolicy, SettlementPolicy
from island_quant.backtest.targets import (
    PinnedTargetStrategy,
    PredictionTargetBuilder,
    PredictionTargetPolicy,
    RebalanceFrequency,
    RebalancePolicy,
    TargetPolicyKind,
)
from island_quant.domain.models import Instrument, Market
from island_quant.execution.simulation import ExecutionQuote
from island_quant.pipeline.artifacts import ExactArtifactStore, canonical_json
from island_quant.pipeline.coverage import CoverageReport
from island_quant.pipeline.orchestration import (
    STAGES,
    CheckpointedPipeline,
    PipelinePlan,
    PipelineRun,
    ResourcePolicy,
)
from island_quant.pipeline.predictions import PredictionEligibility, SelectedOOSPredictionReader
from island_quant.storage.provenance import capture_code_provenance

TAIPEI = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True, slots=True)
class ExploratoryPipelineConfig:
    cache_root: Path
    artifact_root: Path
    state_root: Path
    instruments: tuple[str, ...]
    start: date
    end: date
    chunk_size: int = 1000
    maximum_instruments: int = 50
    maximum_date_range_days: int = 730
    maximum_rows: int = 1_000_000

    @classmethod
    def load(cls, path: Path) -> ExploratoryPipelineConfig:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("pipeline config must be a mapping")
        allowed = {
            "cache_root",
            "artifact_root",
            "state_root",
            "instruments",
            "start",
            "end",
            "chunk_size",
            "maximum_instruments",
            "maximum_date_range_days",
            "maximum_rows",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"unknown pipeline config keys: {sorted(unknown)}")
        return cls(
            Path(raw["cache_root"]),
            Path(raw["artifact_root"]),
            Path(raw["state_root"]),
            tuple(str(value) for value in raw["instruments"]),
            date.fromisoformat(str(raw["start"])),
            date.fromisoformat(str(raw["end"])),
            int(raw.get("chunk_size", 1000)),
            int(raw.get("maximum_instruments", 50)),
            int(raw.get("maximum_date_range_days", 730)),
            int(raw.get("maximum_rows", 1_000_000)),
        )

    @property
    def resource_policy(self) -> ResourcePolicy:
        return ResourcePolicy(
            self.chunk_size,
            self.maximum_instruments,
            self.maximum_date_range_days,
            self.maximum_rows,
        )


class RealCacheExploratoryPipeline:
    """Materialize a deterministic bounded pipeline from existing legal local cache files."""

    def __init__(self, config: ExploratoryPipelineConfig) -> None:
        self.config = config
        self._bars: list[dict[str, object]] | None = None

    def plan(self, *, confirm_large_run: bool = False) -> PipelinePlan:
        if not self.config.instruments or len(set(self.config.instruments)) != len(
            self.config.instruments
        ):
            raise ValueError("pipeline instruments must be non-empty and unique")
        if self.config.end < self.config.start:
            raise ValueError("pipeline end must not precede start")
        estimated = len(self.config.instruments) * ((self.config.end - self.config.start).days + 1)
        self.config.resource_policy.check(
            len(self.config.instruments),
            self.config.start,
            self.config.end,
            estimated,
            confirm_large_run,
        )
        missing = [
            symbol for symbol in self.config.instruments if not self._source_path(symbol).is_file()
        ]
        if missing:
            raise FileNotFoundError(
                f"missing cached market data for instruments: {','.join(missing)}"
            )
        return PipelinePlan(
            "existing_local_cache",
            self.config.instruments,
            self.config.start,
            self.config.end,
            estimated,
            STAGES,
            "real_exploratory",
        )

    def run(self, *, confirm_large_run: bool = False) -> PipelineRun:
        self.plan(confirm_large_run=confirm_large_run)
        bars = self._load_bars()
        if len(bars) > self.config.maximum_rows and not confirm_large_run:
            raise RuntimeError("actual pipeline rows exceed safety limit; pass --confirm-large-run")
        coverage = _coverage(bars, self.config)
        stores = {name: ExactArtifactStore(self.config.artifact_root, name, 1) for name in STAGES}
        created = datetime(2026, 9, 19, tzinfo=TAIPEI)
        source_checksum = self._source_checksum()
        code_provenance = capture_code_provenance()
        code_checksum = code_provenance.source_tree_hash

        def publish(name: str, records: list[dict[str, object]], lineage: dict[str, str]) -> str:
            return (
                stores[name]
                .publish(
                    records,
                    lineage={**lineage, "pipeline_code": code_checksum},
                    completeness="incomplete",
                    classification="real_exploratory",
                    created_at=created,
                    write_batch_size=self.config.chunk_size,
                )
                .artifact_version
            )

        def market(_: dict[str, str]) -> str:
            return publish("normalized_market_data", bars, {"source_cache": source_checksum})

        def universe(outputs: dict[str, str]) -> str:
            listing = {
                symbol: min(
                    date.fromisoformat(str(row["trade_date"]))
                    for row in bars
                    if row["instrument_id"] == f"TWSE:{symbol}"
                )
                for symbol in self.config.instruments
            }
            records = [
                {
                    "instrument_id": row["instrument_id"],
                    "trade_date": row["trade_date"],
                    "eligible": True,
                    "exclusion_reasons": [],
                    "listing_date": listing[str(row["instrument_id"]).split(":", 1)[1]].isoformat(),
                    "delisting_date": None,
                }
                for row in bars
            ]
            return publish(
                "pit_universe", records, {"market_data": outputs["normalized_market_data"]}
            )

        def features(outputs: dict[str, str]) -> str:
            grouped = _group_bars(bars)
            records: list[dict[str, object]] = []
            for instrument, rows in grouped.items():
                for index, row in enumerate(rows):
                    valid = index >= 5
                    value = (
                        Decimal(str(row["close"])) / Decimal(str(rows[index - 5]["close"])) - 1
                        if valid
                        else None
                    )
                    records.append(
                        {
                            "instrument_id": instrument,
                            "decision_time": _decision(row["trade_date"]).isoformat(),
                            "feature_name": "momentum_5",
                            "value": value,
                            "valid": valid,
                        }
                    )
            return publish(
                "features",
                records,
                {
                    "market_data": outputs["normalized_market_data"],
                    "universe": outputs["pit_universe"],
                },
            )

        def labels(outputs: dict[str, str]) -> str:
            records: list[dict[str, object]] = []
            for instrument, rows in _group_bars(bars).items():
                for index, row in enumerate(rows):
                    valid = index + 1 < len(rows)
                    following = rows[index + 1] if valid else row
                    value = (
                        Decimal(str(following["close"])) / Decimal(str(following["open"])) - 1
                        if valid
                        else None
                    )
                    records.append(
                        {
                            "instrument_id": instrument,
                            "decision_time": _decision(row["trade_date"]).isoformat(),
                            "label_version": "next-session-o2c-v1",
                            "value": value,
                            "valid": valid,
                        }
                    )
            return publish(
                "labels",
                records,
                {
                    "market_data": outputs["normalized_market_data"],
                    "universe": outputs["pit_universe"],
                },
            )

        def ml(outputs: dict[str, str]) -> str:
            return publish(
                "walk_forward_ml",
                [
                    {
                        "model_family": "expanding-linear-baseline",
                        "selection": "predeclared",
                        "fit_semantics": "prior_sessions_only",
                    }
                ],
                {"features": outputs["features"], "labels": outputs["labels"]},
            )

        def predictions(outputs: dict[str, str]) -> str:
            records: list[dict[str, object]] = []
            grouped = _group_bars(bars)
            common_days = sorted(
                set.intersection(
                    *(
                        {date.fromisoformat(str(row["trade_date"])) for row in rows}
                        for rows in grouped.values()
                    )
                )
            )
            indexed = {
                instrument: {date.fromisoformat(str(row["trade_date"])): row for row in rows}
                for instrument, rows in grouped.items()
            }
            for index in range(7, len(common_days) - 3):
                decision_day = common_days[index]
                training: list[tuple[Decimal, Decimal]] = []
                for prior_index in range(5, index - 1):
                    day = common_days[prior_index]
                    next_day = common_days[prior_index + 1]
                    lookback_day = common_days[prior_index - 5]
                    for instrument in sorted(grouped):
                        feature = (
                            Decimal(str(indexed[instrument][day]["close"]))
                            / Decimal(str(indexed[instrument][lookback_day]["close"]))
                            - 1
                        )
                        label = (
                            Decimal(str(indexed[instrument][next_day]["close"]))
                            / Decimal(str(indexed[instrument][next_day]["open"]))
                            - 1
                        )
                        training.append((feature, label))
                intercept, coefficient = _fit_expanding_linear(training)
                decision = _decision(decision_day)
                fit_cutoff = _decision(common_days[index - 1])
                for instrument in sorted(grouped):
                    current = indexed[instrument][decision_day]
                    lookback = indexed[instrument][common_days[index - 5]]
                    feature = Decimal(str(current["close"])) / Decimal(str(lookback["close"])) - 1
                    value = intercept + coefficient * feature
                    payload: dict[str, object] = {
                        "strategy_run": "real-cache-exploratory-v1",
                        "experiment_id": "expanding-baseline-v1",
                        "fold_id": f"fold-{decision_day.isoformat()}",
                        "model_artifact_version": outputs["walk_forward_ml"],
                        "feature_artifact_version": outputs["features"],
                        "label_version": "next-session-o2c-v1",
                        "dataset_version": outputs["normalized_market_data"],
                        "universe_version": outputs["pit_universe"],
                        "instrument_id": instrument,
                        "decision_time": decision.isoformat(),
                        "available_at": (decision - timedelta(minutes=1)).isoformat(),
                        "fit_cutoff": fit_cutoff.isoformat(),
                        "prediction": value,
                        "target_definition": "next-session-open-to-close-return",
                        "split_role": "test",
                        "completeness_status": "exploratory",
                        "holdout_access_audit": None,
                    }
                    payload["checksum"] = hashlib.sha256(
                        canonical_json({**payload, "checksum": ""})
                    ).hexdigest()
                    records.append(payload)
            return publish(
                "selected_oos_predictions",
                records,
                {
                    "dataset_version": outputs["normalized_market_data"],
                    "universe_version": outputs["pit_universe"],
                    "model": outputs["walk_forward_ml"],
                },
            )

        def passthrough(name: str, source: str):  # type: ignore[no-untyped-def]
            def runner(outputs: dict[str, str]) -> str:
                return publish(
                    name,
                    [
                        {
                            "stage": name,
                            "source_version": outputs[source],
                            "classification": "real_exploratory",
                        }
                    ],
                    {source: outputs[source]},
                )

            return runner

        def backtest(outputs: dict[str, str]) -> str:
            return _run_event_backtest(
                self.config.artifact_root,
                bars,
                outputs["normalized_market_data"],
                outputs["pit_universe"],
                outputs["selected_oos_predictions"],
                created,
            )

        runners = {
            "normalized_market_data": market,
            "pit_universe": universe,
            "features": features,
            "labels": labels,
            "walk_forward_ml": ml,
            "selected_oos_predictions": predictions,
            "targets": passthrough("targets", "selected_oos_predictions"),
            "event_driven_backtest": backtest,
            "analytics_attribution": passthrough("analytics_attribution", "event_driven_backtest"),
            "immutable_artifacts": passthrough("immutable_artifacts", "analytics_attribution"),
        }
        orchestrator = CheckpointedPipeline(self.config.artifact_root, self.config.state_root)

        def stage_validator(stage: str) -> Callable[[str], None]:
            def validate(version: str) -> None:
                stores[stage].manifest(version)

            return validate

        validators = {
            name: stage_validator(name) for name in STAGES if name != "event_driven_backtest"
        }

        def validate_backtest(version: str) -> None:
            BacktestArtifactStore(self.config.artifact_root).read(version)

        validators["event_driven_backtest"] = validate_backtest
        return orchestrator.execute(
            config={
                **_config_identity(self.config),
                "git_commit": code_provenance.git_commit,
                "dirty": code_provenance.dirty,
                "source_tree_hash": code_checksum,
            },
            coverage=coverage,
            runners=runners,
            backtest_version=lambda outputs: outputs["event_driven_backtest"],
            validators=validators,
        )

    def _load_bars(self) -> list[dict[str, object]]:
        if self._bars is not None:
            return self._bars
        rows: list[dict[str, object]] = []
        for symbol in self.config.instruments:
            payload = json.loads(self._source_path(symbol).read_text(encoding="utf-8"))
            for source in payload.get("data", []):
                day = date.fromisoformat(source["date"])
                if not self.config.start <= day <= self.config.end:
                    continue
                rows.append(
                    {
                        "instrument_id": f"TWSE:{symbol}",
                        "market": "TWSE",
                        "trade_date": day.isoformat(),
                        "event_time": datetime.combine(day, time(13, 30), TAIPEI).isoformat(),
                        "available_at": datetime.combine(day, time(18), TAIPEI).isoformat(),
                        "open": Decimal(str(source["open"])),
                        "high": Decimal(str(source["max"])),
                        "low": Decimal(str(source["min"])),
                        "close": Decimal(str(source["close"])),
                        "volume": int(source["Trading_Volume"]),
                    }
                )
        rows.sort(key=lambda item: (str(item["trade_date"]), str(item["instrument_id"])))
        if not rows:
            raise RuntimeError("cached sources contain no rows in requested range")
        self._bars = rows
        return rows

    def _source_path(self, symbol: str) -> Path:
        matches = sorted(
            self.config.cache_root.glob(f"finmind_{symbol}_*.json"),
            key=lambda path: len(path.name),
            reverse=True,
        )
        if not matches:
            return self.config.cache_root / f"missing-{symbol}.json"
        return matches[0]

    def _source_checksum(self) -> str:
        digest = hashlib.sha256()
        for symbol in sorted(self.config.instruments):
            path = self._source_path(symbol)
            digest.update(symbol.encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()


def _decision(value: object) -> datetime:
    return datetime.combine(date.fromisoformat(str(value)), time(18), TAIPEI)


def _group_bars(bars: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {}
    for row in bars:
        result.setdefault(str(row["instrument_id"]), []).append(row)
    for rows in result.values():
        rows.sort(key=lambda item: str(item["trade_date"]))
    return result


def _coverage(bars: list[dict[str, object]], config: ExploratoryPipelineConfig) -> CoverageReport:
    days = {str(row["trade_date"]) for row in bars}
    instruments = {str(row["instrument_id"]) for row in bars}
    blockers = (
        "provisional_listing_dates",
        "missing_delisted_history",
        "corporate_action_feed_missing",
        "suspension_history_missing",
        "price_limit_tradability_missing",
        "benchmark_missing",
        "market_cap_missing",
        "incomplete_point_in_time_reference_data",
    )
    return CoverageReport.create(
        date_coverage=f"{min(days)}..{max(days)} ({len(days)} observed sessions)",
        instrument_coverage=f"{len(instruments)}/{len(config.instruments)} requested instruments",
        unknown_market=0,
        provisional_listing_date=len(instruments),
        missing_delisted_history=True,
        corporate_action_coverage="missing",
        suspension_coverage="missing",
        price_limit_tradability_coverage="missing",
        benchmark_coverage="missing",
        market_cap_coverage="missing",
        feature_validity="partial_lookback",
        label_validity="partial_terminal_boundary",
        prediction_coverage="bounded_OOS_after_warmup",
        backtest_valuation_completeness="exploratory",
        pit_research_completeness="incomplete",
        promotion_blockers=blockers,
    )


def _config_identity(config: ExploratoryPipelineConfig) -> dict[str, Any]:
    payload = asdict(config)
    return {
        key: str(value) if isinstance(value, (Path, date)) else value
        for key, value in payload.items()
        if key not in {"artifact_root", "state_root", "cache_root"}
    }


def _fit_expanding_linear(
    observations: list[tuple[Decimal, Decimal]],
) -> tuple[Decimal, Decimal]:
    if len(observations) < 2:
        raise RuntimeError("expanding baseline requires at least two prior observations")
    count = Decimal(len(observations))
    mean_x = sum((item[0] for item in observations), Decimal("0")) / count
    mean_y = sum((item[1] for item in observations), Decimal("0")) / count
    variance = sum(((x - mean_x) ** 2 for x, _ in observations), Decimal("0"))
    if variance == 0:
        return mean_y, Decimal("0")
    covariance = sum(((x - mean_x) * (y - mean_y) for x, y in observations), Decimal("0"))
    coefficient = covariance / variance
    return mean_y - coefficient * mean_x, coefficient


def _run_event_backtest(
    artifact_root: Path,
    bars: list[dict[str, object]],
    dataset_version: str,
    universe_version: str,
    prediction_version: str,
    created_at: datetime,
) -> str:
    grouped = _group_bars(bars)
    common_days = sorted(
        set.intersection(
            *(
                {date.fromisoformat(str(row["trade_date"])) for row in rows}
                for rows in grouped.values()
            )
        )
    )
    if len(common_days) < 12:
        raise RuntimeError("not enough common sessions for exploratory backtest")
    instruments = {key: Instrument(key.split(":", 1)[1], Market.TWSE) for key in sorted(grouped)}
    eligible = frozenset((key, day) for key in instruments for day in common_days)
    boundaries: dict[str, tuple[date, date | None]] = {
        key: (common_days[0], None) for key in instruments
    }
    ledger = SelectedOOSPredictionReader(artifact_root).read(
        prediction_version,
        PredictionEligibility(eligible, boundaries, dataset_version, universe_version),
    )
    by_key_day = {
        (str(row["instrument_id"]), date.fromisoformat(str(row["trade_date"]))): row for row in bars
    }
    decisions = sorted({row.decision_time.date() for row in ledger.records})
    sessions: list[BacktestSession] = []
    used_decisions: list[datetime] = []
    for decision_day in decisions:
        if decision_day not in common_days:
            continue
        index = common_days.index(decision_day)
        if index + 1 >= len(common_days):
            continue
        trade_day = common_days[index + 1]
        decision_time = datetime.combine(decision_day, time(18), TAIPEI)
        execution_time = datetime.combine(trade_day, time(9), TAIPEI)
        close_time = datetime.combine(trade_day, time(13, 30), TAIPEI)
        predictions = ledger.at(decision_time)
        if len(predictions) != len(instruments):
            continue
        quotes = tuple(
            ExecutionQuote(
                instrument,
                execution_time,
                Decimal(str(by_key_day[(key, trade_day)]["open"])),
                Decimal(str(by_key_day[(key, trade_day)]["close"])),
                int(str(by_key_day[(key, trade_day)]["volume"])),
            )
            for key, instrument in instruments.items()
        )
        observations = tuple(
            PredictionObservation(
                instruments[row.instrument_id],
                row.prediction or Decimal("0"),
                row.available_at,
                prediction_version,
            )
            for row in predictions
        )
        sessions.append(
            BacktestSession(
                trade_day,
                decision_time,
                execution_time,
                close_time,
                quotes,
                tuple(
                    (
                        key,
                        Decimal(str(by_key_day[(key, decision_day)]["close"])),
                    )
                    for key in instruments
                ),
                observations,
            )
        )
        used_decisions.append(decision_time)
    if not sessions:
        raise RuntimeError("selected prediction coverage produced no executable sessions")
    policy = PredictionTargetPolicy(
        version="real-exploratory-top-k-v1",
        kind=TargetPolicyKind.TOP_K,
        cash_buffer=Decimal("0.10"),
        maximum_position_weight=Decimal("0.45"),
        top_k=min(2, len(instruments)),
    )
    artifacts = []
    targets = []
    builder = PredictionTargetBuilder()
    for decision_time in used_decisions:
        target_artifact, positions = builder.build(
            ledger.at(decision_time),
            instruments,
            strategy_version="real-cache-expanding-baseline-v1",
            universe_version=universe_version,
            policy=policy,
        )
        artifacts.append(target_artifact)
        targets.append((decision_time, positions))
    strategy = PinnedTargetStrategy(
        "real-cache-expanding-baseline-v1",
        "real-cache-expanding-baseline-v1",
        tuple(artifacts),
        tuple(targets),
        RebalancePolicy("daily-real-exploratory-v1", RebalanceFrequency.DAILY),
        tuple(session.trade_date for session in sessions),
        tuple(instruments.values()),
    )
    result = EventDrivenBacktestEngine().run(
        BacktestConfig(
            run_id=f"real-exploratory-{prediction_version[:16]}",
            portfolio_id="real-exploratory-twd-cash",
            initial_cash=Decimal("1000000"),
            trading_calendar=tuple(day for day in common_days if day >= sessions[0].trade_date),
            fee_policy=FeeTaxPolicy(),
            settlement_policy=SettlementPolicy(),
            fill_policy=FillPolicy(maximum_volume_participation=Decimal("0.01")),
            risk_policy=RiskPolicy(maximum_position_weight=Decimal("0.50")),
            pit_reference_complete=False,
        ),
        tuple(sessions),
        strategy,
    )
    performance = PerformanceAnalyzer().analyze(result, PerformancePolicy())
    attribution = attribute(result, performance)
    store = BacktestArtifactStore(artifact_root)
    backtest_artifact = store.build(
        result,
        performance,
        attribution,
        (),
        target_policy_version=policy.version,
        prediction_artifact_version=prediction_version,
        model_artifact_version=dict(
            store_manifest_lineage(artifact_root, "selected_oos_predictions", prediction_version)
        )["model"],
        dataset_version=dataset_version,
        universe_version=universe_version,
        calendar_version=hashlib.sha256(canonical_json(common_days)).hexdigest(),
        corporate_action_version=hashlib.sha256(b"no-verified-corporate-action-feed").hexdigest(),
        benchmark_version=hashlib.sha256(b"benchmark-unavailable").hexdigest(),
        benchmark=None,
        seed=42,
        created_time=created_at,
        completeness_status="exploratory",
        synthetic_demo=False,
        config={
            "source": "existing_local_cache",
            "classification": "real_exploratory",
            "warning": "EXPLORATORY — INCOMPLETE POINT-IN-TIME REFERENCE DATA",
        },
        input_checksums={
            "market_data": dataset_version,
            "universe": universe_version,
            "predictions": ledger.checksum,
        },
    )
    store.save(backtest_artifact)
    return backtest_artifact.manifest.artifact_version


def store_manifest_lineage(
    root: Path, artifact_type: str, version: str
) -> tuple[tuple[str, str], ...]:
    return ExactArtifactStore(root, artifact_type, 1).manifest(version).lineage
