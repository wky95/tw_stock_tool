"""Strict selected-OOS prediction filesystem adapter."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from island_quant.backtest.predictions import (
    OOSPrediction,
    PredictionRole,
    SelectedPredictionLedger,
)
from island_quant.pipeline.artifacts import ExactArtifactStore, canonical_json


@dataclass(frozen=True, slots=True)
class PredictionEligibility:
    eligible: frozenset[tuple[str, date]]
    listing_boundaries: dict[str, tuple[date, date | None]]
    dataset_version: str
    universe_version: str


@dataclass(frozen=True, slots=True)
class SelectedPredictionRecord:
    strategy_run: str
    experiment_id: str
    fold_id: str
    model_artifact_version: str
    feature_artifact_version: str
    label_version: str
    dataset_version: str
    universe_version: str
    instrument_id: str
    decision_time: datetime
    available_at: datetime
    fit_cutoff: datetime
    prediction: Decimal | None
    target_definition: str
    split_role: PredictionRole
    completeness_status: str
    holdout_access_audit: str | None
    checksum: str

    def expected_checksum(self) -> str:
        payload = {
            **asdict(self),
            "decision_time": self.decision_time.isoformat(),
            "available_at": self.available_at.isoformat(),
            "fit_cutoff": self.fit_cutoff.isoformat(),
            "split_role": self.split_role.value,
            "checksum": "",
        }
        return hashlib.sha256(canonical_json(payload)).hexdigest()


class SelectedOOSPredictionReader:
    artifact_type = "selected_oos_predictions"
    schema_version = 1

    def __init__(self, root: Path) -> None:
        self.store = ExactArtifactStore(root, self.artifact_type, self.schema_version)

    def read(
        self,
        version: str,
        eligibility: PredictionEligibility,
        *,
        batch_size: int = 1000,
    ) -> SelectedPredictionLedger:
        manifest = self.store.manifest(version)
        lineage = dict(manifest.lineage)
        if lineage.get("dataset_version") != eligibility.dataset_version:
            raise ValueError("prediction dataset lineage mismatch")
        if lineage.get("universe_version") != eligibility.universe_version:
            raise ValueError("prediction universe lineage mismatch")
        rows: list[SelectedPredictionRecord] = []
        ownership: set[tuple[str, datetime]] = set()
        decision_fold: dict[datetime, str] = {}
        for batch in self.store.batches(version, batch_size=batch_size):
            for item in batch.records:
                record = _record(item)
                if record.checksum != record.expected_checksum():
                    raise ValueError("selected prediction row checksum mismatch")
                if record.dataset_version != eligibility.dataset_version:
                    raise ValueError("mixed prediction dataset versions")
                if record.universe_version != eligibility.universe_version:
                    raise ValueError("mixed prediction universe versions")
                if record.split_role is PredictionRole.TRAIN:
                    raise ValueError("in-sample predictions are forbidden")
                if record.split_role is PredictionRole.VALIDATION:
                    raise ValueError("candidate validation predictions are forbidden")
                if (
                    record.split_role is PredictionRole.FINAL_HOLDOUT
                    and not record.holdout_access_audit
                ):
                    raise ValueError("final holdout prediction lacks access audit")
                if record.fit_cutoff >= record.decision_time:
                    raise ValueError("fit cutoff must precede decision time")
                if record.available_at > record.decision_time:
                    raise ValueError("prediction was unavailable at decision time")
                key = (record.instrument_id, record.decision_time)
                if key in ownership:
                    raise ValueError("duplicate OOS prediction ownership")
                ownership.add(key)
                prior = decision_fold.setdefault(record.decision_time, record.fold_id)
                if prior != record.fold_id:
                    raise ValueError("overlapping OOS fold ownership")
                if (record.instrument_id, record.decision_time.date()) not in eligibility.eligible:
                    raise ValueError("prediction is outside PIT eligible universe")
                if record.instrument_id not in eligibility.listing_boundaries:
                    raise ValueError("prediction lacks listing boundary")
                listed, delisted = eligibility.listing_boundaries[record.instrument_id]
                day = record.decision_time.date()
                if day < listed or (delisted is not None and day > delisted):
                    raise ValueError("prediction is outside listing boundary")
                rows.append(record)
        if not rows:
            raise ValueError("selected prediction artifact is empty")
        predictions = tuple(
            OOSPrediction.create(
                strategy_run=row.strategy_run,
                experiment_id=row.experiment_id,
                fold_id=row.fold_id,
                model_artifact_version=row.model_artifact_version,
                feature_artifact_version=row.feature_artifact_version,
                label_version=row.label_version,
                prediction_artifact_version=version,
                instrument_id=row.instrument_id,
                decision_time=row.decision_time,
                available_at=row.available_at,
                prediction=row.prediction,
                target_definition=row.target_definition,
                split_role=row.split_role,
                fit_cutoff=row.fit_cutoff,
                completeness_status=row.completeness_status,
            )
            for row in rows
        )
        audits = {
            row.holdout_access_audit
            for row in rows
            if row.split_role is PredictionRole.FINAL_HOLDOUT
        }
        audit = next(iter(audits)) if len(audits) == 1 else None
        return SelectedPredictionLedger.build(
            version,
            predictions,
            eligible_membership=set(eligibility.eligible),
            listing_boundaries=eligibility.listing_boundaries,
            holdout_access_audit=audit,
        )


def _record(item: dict[str, object]) -> SelectedPredictionRecord:
    return SelectedPredictionRecord(
        strategy_run=str(item["strategy_run"]),
        experiment_id=str(item["experiment_id"]),
        fold_id=str(item["fold_id"]),
        model_artifact_version=str(item["model_artifact_version"]),
        feature_artifact_version=str(item["feature_artifact_version"]),
        label_version=str(item["label_version"]),
        dataset_version=str(item["dataset_version"]),
        universe_version=str(item["universe_version"]),
        instrument_id=str(item["instrument_id"]),
        decision_time=datetime.fromisoformat(str(item["decision_time"])),
        available_at=datetime.fromisoformat(str(item["available_at"])),
        fit_cutoff=datetime.fromisoformat(str(item["fit_cutoff"])),
        prediction=Decimal(str(item["prediction"])) if item.get("prediction") is not None else None,
        target_definition=str(item["target_definition"]),
        split_role=PredictionRole(str(item["split_role"])),
        completeness_status=str(item["completeness_status"]),
        holdout_access_audit=str(item["holdout_access_audit"])
        if item.get("holdout_access_audit")
        else None,
        checksum=str(item["checksum"]),
    )
