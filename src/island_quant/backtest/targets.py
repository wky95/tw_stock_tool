"""Versioned prediction-to-target and trading-session rebalance policies."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from island_quant.backtest.contracts import BacktestSession
from island_quant.backtest.predictions import OOSPrediction
from island_quant.domain.models import Instrument, TargetPosition

ZERO = Decimal("0")


class TargetPolicyKind(StrEnum):
    TOP_K = "top_k_equal_weight"
    QUANTILE = "quantile_long_only"
    THRESHOLD = "threshold_long_only"


class InsufficientMembersPolicy(StrEnum):
    USE_AVAILABLE = "use_available"
    HOLD_CASH = "hold_cash"


@dataclass(frozen=True, slots=True)
class PredictionTargetPolicy:
    version: str
    kind: TargetPolicyKind
    cash_buffer: Decimal = Decimal("0.05")
    maximum_position_weight: Decimal = Decimal("0.20")
    top_k: int = 5
    top_quantile: Decimal = Decimal("0.20")
    threshold: Decimal = ZERO
    insufficient_members: InsufficientMembersPolicy = InsufficientMembersPolicy.USE_AVAILABLE

    def __post_init__(self) -> None:
        if not self.version or self.version in {"latest", "current"}:
            raise ValueError("target policy requires an exact version")
        if not ZERO <= self.cash_buffer < Decimal("1"):
            raise ValueError("cash buffer must be in [0, 1)")
        if not ZERO < self.maximum_position_weight <= Decimal("1"):
            raise ValueError("maximum position weight must be in (0, 1]")
        if self.top_k < 1 or not ZERO < self.top_quantile <= Decimal("1"):
            raise ValueError("top-k and quantile settings are invalid")


@dataclass(frozen=True, slots=True)
class TargetMember:
    instrument_id: str
    rank: int | None
    prediction: Decimal | None
    target_weight: Decimal
    exclusion_reason: str | None


@dataclass(frozen=True, slots=True)
class TargetArtifact:
    strategy_version: str
    prediction_artifact_version: str
    universe_version: str
    decision_time: datetime
    policy_version: str
    policy_config: dict[str, str]
    members: tuple[TargetMember, ...]
    checksum: str


class PredictionTargetBuilder:
    def build(
        self,
        predictions: tuple[OOSPrediction, ...],
        instruments: dict[str, Instrument],
        *,
        strategy_version: str,
        universe_version: str,
        policy: PredictionTargetPolicy,
    ) -> tuple[TargetArtifact, tuple[TargetPosition, ...]]:
        if not predictions:
            raise ValueError("target construction requires pinned predictions")
        if strategy_version in {"", "latest", "current"} or universe_version in {
            "",
            "latest",
            "current",
        }:
            raise ValueError("target construction requires exact strategy and universe versions")
        decision_time = predictions[0].decision_time
        if any(item.decision_time != decision_time for item in predictions):
            raise ValueError("target construction accepts one decision time")
        prediction_versions = {item.prediction_artifact_version for item in predictions}
        if len(prediction_versions) != 1:
            raise ValueError("mixed prediction artifact versions are forbidden")
        prediction_ids = [item.instrument_id for item in predictions]
        if len(prediction_ids) != len(set(prediction_ids)):
            raise ValueError("duplicate prediction instruments are forbidden")
        eligible = [
            item
            for item in predictions
            if item.prediction is not None and item.instrument_id in instruments
        ]
        ranked = sorted(
            eligible,
            key=lambda item: (-item.prediction, item.instrument_id),  # type: ignore[operator]
        )
        selected_count = self._selected_count(policy, ranked)
        if (
            policy.kind is TargetPolicyKind.TOP_K
            and len(ranked) < policy.top_k
            and policy.insufficient_members is InsufficientMembersPolicy.HOLD_CASH
        ):
            selected_count = 0
        selected = ranked[:selected_count]
        selected_ids = {item.instrument_id for item in selected}
        investable = Decimal("1") - policy.cash_buffer
        raw_weight = investable / len(selected) if selected else ZERO
        weight = min(raw_weight, policy.maximum_position_weight)
        ranks = {item.instrument_id: rank for rank, item in enumerate(ranked, start=1)}
        members: list[TargetMember] = []
        targets: list[TargetPosition] = []
        for item in sorted(predictions, key=lambda value: value.instrument_id):
            if item.instrument_id not in instruments:
                reason = "data_quality:ineligible_instrument"
            elif item.prediction is None:
                reason = "data_quality:invalid_prediction"
            elif item.instrument_id not in selected_ids:
                reason = "strategy_signal:not_selected_by_policy"
            else:
                reason = None
            target_weight = weight if reason is None else ZERO
            members.append(
                TargetMember(
                    item.instrument_id,
                    ranks.get(item.instrument_id),
                    item.prediction,
                    target_weight,
                    reason,
                )
            )
            if item.instrument_id in instruments:
                targets.append(
                    TargetPosition(
                        strategy_version,
                        instruments[item.instrument_id],
                        decision_time,
                        target_weight,
                        (
                            f"{policy.kind.value}:{reason or 'selected'}:"
                            f"{item.available_at.isoformat()}"
                        ),
                    )
                )
        config = {key: str(value) for key, value in asdict(policy).items()}
        payload = {
            "strategy_version": strategy_version,
            "prediction_artifact_version": predictions[0].prediction_artifact_version,
            "universe_version": universe_version,
            "decision_time": decision_time.isoformat(),
            "policy_version": policy.version,
            "policy_config": config,
            "members": [asdict(item) for item in members],
        }
        checksum = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        return (
            TargetArtifact(
                strategy_version,
                predictions[0].prediction_artifact_version,
                universe_version,
                decision_time,
                policy.version,
                config,
                tuple(members),
                checksum,
            ),
            tuple(targets),
        )

    @staticmethod
    def _selected_count(
        policy: PredictionTargetPolicy, ranked: list[OOSPrediction]
    ) -> int:
        if policy.kind is TargetPolicyKind.TOP_K:
            return min(policy.top_k, len(ranked))
        if policy.kind is TargetPolicyKind.QUANTILE:
            return min(len(ranked), math.ceil(len(ranked) * float(policy.top_quantile)))
        return sum(
            item.prediction is not None and item.prediction > policy.threshold
            for item in ranked
        )


class RebalanceFrequency(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    N_SESSIONS = "n_sessions"


class MissingPredictionAction(StrEnum):
    LIQUIDATE_TO_CASH = "liquidate_to_cash"
    HOLD_PREVIOUS_TARGET = "hold_previous_target"
    SKIP_REBALANCE = "skip_rebalance"


class CoverageFailureAction(StrEnum):
    FAIL_CLOSED = "fail_closed"
    SKIP_REBALANCE = "skip_rebalance"


@dataclass(frozen=True, slots=True)
class RebalancePolicy:
    version: str
    frequency: RebalanceFrequency
    interval_sessions: int = 1
    missing_prediction_action: MissingPredictionAction = (
        MissingPredictionAction.LIQUIDATE_TO_CASH
    )
    minimum_prediction_coverage: Decimal = Decimal("0.80")
    coverage_failure_action: CoverageFailureAction = CoverageFailureAction.FAIL_CLOSED

    def __post_init__(self) -> None:
        if (
            not self.version
            or self.version in {"latest", "current"}
            or self.interval_sessions < 1
            or not ZERO <= self.minimum_prediction_coverage <= Decimal("1")
        ):
            raise ValueError("rebalance policy is invalid")

    def scheduled_sessions(self, calendar: tuple[date, ...]) -> tuple[date, ...]:
        if self.frequency is RebalanceFrequency.DAILY:
            return calendar
        if self.frequency is RebalanceFrequency.N_SESSIONS:
            return calendar[:: self.interval_sessions]
        selected: list[date] = []
        seen: set[tuple[int, int]] = set()
        for session in calendar:
            iso = session.isocalendar()
            key = (iso.year, iso.week)
            if key not in seen:
                selected.append(session)
                seen.add(key)
        return tuple(selected)

    def is_rebalance(self, session: date, calendar: tuple[date, ...]) -> bool:
        return session in self.scheduled_sessions(calendar)


@dataclass(frozen=True, slots=True)
class PinnedTargetStrategy:
    """Strategy port backed only by already-materialized, immutable targets."""

    strategy_id: str
    version: str
    artifacts: tuple[TargetArtifact, ...]
    targets_by_decision: tuple[tuple[datetime, tuple[TargetPosition, ...]], ...]
    rebalance_policy: RebalancePolicy
    trading_calendar: tuple[date, ...]
    instruments: tuple[Instrument, ...]

    def __post_init__(self) -> None:
        if not self.instruments:
            raise ValueError("pinned target strategy requires a non-empty universe")
        instrument_keys = [instrument.key for instrument in self.instruments]
        if len(instrument_keys) != len(set(instrument_keys)):
            raise ValueError("pinned target strategy universe contains duplicates")
        if tuple(sorted(set(self.trading_calendar))) != self.trading_calendar:
            raise ValueError("pinned trading calendar must be sorted and unique")

    def targets(self, session: BacktestSession) -> tuple[TargetPosition, ...]:
        if not self.rebalance_policy.is_rebalance(session.trade_date, self.trading_calendar):
            return ()
        pinned = dict(self.targets_by_decision).get(session.decision_time)
        valid_keys = {
            member.instrument_id
            for artifact in self.artifacts
            if artifact.decision_time == session.decision_time
            for member in artifact.members
            if member.prediction is not None
            and member.exclusion_reason != "data_quality:ineligible_instrument"
        }
        coverage = Decimal(len(valid_keys)) / len(self.instruments)
        if coverage < self.rebalance_policy.minimum_prediction_coverage:
            if (
                self.rebalance_policy.coverage_failure_action
                is CoverageFailureAction.SKIP_REBALANCE
            ):
                return ()
            raise ValueError("prediction coverage below the pinned fail-closed threshold")
        if pinned is not None:
            existing = {target.instrument.key for target in pinned}
            missing_instruments = tuple(
                instrument for instrument in self.instruments if instrument.key not in existing
            )
            if (
                missing_instruments
                and self.rebalance_policy.missing_prediction_action
                is MissingPredictionAction.SKIP_REBALANCE
            ):
                return ()
            if (
                self.rebalance_policy.missing_prediction_action
                is MissingPredictionAction.LIQUIDATE_TO_CASH
            ):
                missing = tuple(
                    TargetPosition(
                        self.strategy_id,
                        instrument,
                        session.decision_time,
                        ZERO,
                        "data_quality:missing_artifact_row",
                    )
                    for instrument in missing_instruments
                )
                return (*pinned, *missing)
            return pinned
        if (
            self.rebalance_policy.coverage_failure_action
            is CoverageFailureAction.FAIL_CLOSED
        ):
            raise ValueError("prediction coverage is zero for the scheduled decision")
        if self.rebalance_policy.coverage_failure_action is CoverageFailureAction.SKIP_REBALANCE:
            return ()
        raise AssertionError("coverage failure policy is exhaustive")
