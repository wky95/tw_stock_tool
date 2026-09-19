"""Pinned selected-OOS prediction contracts for backtest integration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class PredictionRole(StrEnum):
    TRAIN = "train"
    TEST = "test"
    VALIDATION = "validation"
    FINAL_HOLDOUT = "final_holdout"


@dataclass(frozen=True, slots=True)
class OOSPrediction:
    strategy_run: str
    experiment_id: str
    fold_id: str
    model_artifact_version: str
    feature_artifact_version: str
    label_version: str
    prediction_artifact_version: str
    instrument_id: str
    decision_time: datetime
    available_at: datetime
    prediction: Decimal | None
    target_definition: str
    split_role: PredictionRole
    fit_cutoff: datetime
    completeness_status: str
    checksum: str

    def __post_init__(self) -> None:
        for value in (self.decision_time, self.available_at, self.fit_cutoff):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("prediction timestamps must be timezone-aware")
        versions = {
            self.model_artifact_version,
            self.feature_artifact_version,
            self.label_version,
            self.prediction_artifact_version,
        }
        if versions & {"", "latest", "current"}:
            raise ValueError("prediction inputs require exact pinned versions")
        if self.fit_cutoff >= self.decision_time:
            raise ValueError("prediction fit cutoff must precede decision time")
        if self.available_at > self.decision_time:
            raise ValueError("prediction is unavailable at decision time")
        if self.prediction is not None and not self.prediction.is_finite():
            raise ValueError("prediction must be finite or explicitly missing")
        if self.checksum != self.expected_checksum():
            raise ValueError("prediction checksum mismatch")

    def expected_checksum(self) -> str:
        payload = {**asdict(self), "checksum": ""}
        return hashlib.sha256(_canonical(payload)).hexdigest()

    @classmethod
    def create(cls, **values: object) -> OOSPrediction:
        checksum = hashlib.sha256(_canonical({**values, "checksum": ""})).hexdigest()
        return cls(checksum=checksum, **values)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class SelectedPredictionLedger:
    version: str
    records: tuple[OOSPrediction, ...]
    engineering_validation: bool
    checksum: str

    @classmethod
    def build(
        cls,
        version: str,
        records: tuple[OOSPrediction, ...],
        *,
        eligible_membership: set[tuple[str, date]],
        listing_boundaries: dict[str, tuple[date, date | None]],
        allow_validation_fixture: bool = False,
        holdout_access_audit: str | None = None,
    ) -> SelectedPredictionLedger:
        if version in {"", "latest", "current"} or not records:
            raise ValueError("an exact non-empty prediction ledger version is required")
        owners: set[tuple[str, str, datetime]] = set()
        fold_owners: dict[datetime, str] = {}
        engineering_validation = False
        contracts: set[tuple[str, str, str, str, str, str]] = set()
        for record in records:
            if record.prediction_artifact_version != version:
                raise ValueError("prediction record references another artifact version")
            if not record.completeness_status:
                raise ValueError("prediction completeness status is required")
            contracts.add(
                (
                    record.strategy_run,
                    record.experiment_id,
                    record.model_artifact_version,
                    record.feature_artifact_version,
                    record.label_version,
                    record.target_definition,
                )
            )
            identity = (record.strategy_run, record.instrument_id, record.decision_time)
            if identity in owners:
                raise ValueError("duplicate OOS prediction ownership")
            owners.add(identity)
            existing_fold = fold_owners.setdefault(record.decision_time, record.fold_id)
            if existing_fold != record.fold_id:
                raise ValueError("overlapping test-fold ownership")
            if record.split_role is PredictionRole.TRAIN:
                raise ValueError("in-sample predictions are forbidden")
            if record.split_role is PredictionRole.VALIDATION:
                if not allow_validation_fixture:
                    raise ValueError("validation predictions are not formal OOS backtest inputs")
                engineering_validation = True
            if record.split_role is PredictionRole.FINAL_HOLDOUT and not holdout_access_audit:
                raise ValueError("holdout predictions require an explicit access audit")
            if (record.instrument_id, record.decision_time.date()) not in eligible_membership:
                raise ValueError("prediction instrument is outside the eligible universe")
            try:
                listed, delisted = listing_boundaries[record.instrument_id]
            except KeyError as exc:
                raise ValueError("prediction instrument lacks listing history") from exc
            day = record.decision_time.date()
            if day < listed or (delisted is not None and day > delisted):
                raise ValueError("prediction crosses listing or delisting boundary")
        if len(contracts) != 1:
            raise ValueError("mixed candidate-model or prediction contracts are forbidden")
        ordered = tuple(
            sorted(records, key=lambda item: (item.decision_time, item.instrument_id))
        )
        checksum = hashlib.sha256(_canonical([asdict(item) for item in ordered])).hexdigest()
        return cls(version, ordered, engineering_validation, checksum)

    def at(self, decision_time: datetime) -> tuple[OOSPrediction, ...]:
        return tuple(item for item in self.records if item.decision_time == decision_time)


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
