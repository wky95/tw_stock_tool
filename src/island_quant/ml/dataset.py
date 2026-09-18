"""Deterministic supervised panel construction with exact-time joins."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

import polars as pl


class MissingFeaturePolicy(StrEnum):
    INVALID = "invalid"
    DROP_SAMPLE = "drop_sample"


@dataclass(frozen=True, slots=True)
class TargetContract:
    kind: str
    label_version: str
    return_horizon: int
    entry_definition: str
    exit_definition: str
    return_semantics: str
    benchmark_definition: str | None = None
    rank_direction: str | None = None

    def __post_init__(self) -> None:
        supported = {
            "raw_forward_return",
            "benchmark_relative_forward_return",
            "cross_sectional_rank",
        }
        if self.kind not in supported:
            raise ValueError(f"unsupported target: {self.kind}")
        if self.return_semantics != "gross_before_costs":
            raise ValueError("Slice 4 targets must be explicitly gross_before_costs")
        if self.kind == "benchmark_relative_forward_return" and not self.benchmark_definition:
            raise ValueError("benchmark-relative targets require a benchmark definition")
        if self.kind == "cross_sectional_rank" and not self.rank_direction:
            raise ValueError("rank targets require an explicit rank direction")


@dataclass(frozen=True, slots=True)
class FeatureMatrixSchema:
    names: tuple[str, ...]
    dtypes: tuple[str, ...]

    def validate_columns(self, names: tuple[str, ...]) -> None:
        if names != self.names:
            raise ValueError(f"feature schema order mismatch: expected {self.names}, got {names}")


@dataclass(frozen=True, slots=True)
class SupervisedDatasetManifest:
    version: str
    feature_schema: FeatureMatrixSchema
    feature_set_version: str
    feature_artifact_version: str
    label_version: str
    label_artifact_version: str
    universe_version: str
    dataset_version: str
    availability_policy_version: str
    target: TargetContract
    missing_feature_policy: MissingFeaturePolicy
    universe_size_by_date: tuple[tuple[str, int], ...]
    valid_sample_count_by_date: tuple[tuple[str, int], ...]
    checksum: str


@dataclass(frozen=True, slots=True)
class SupervisedDataset:
    frame: pl.DataFrame
    manifest: SupervisedDatasetManifest


def manifest_from_dict(payload: dict[str, Any]) -> SupervisedDatasetManifest:
    """Reconstruct a manifest from checksum-verified, system-generated JSON."""
    schema = FeatureMatrixSchema(
        tuple(payload["feature_schema"]["names"]),
        tuple(payload["feature_schema"]["dtypes"]),
    )
    target = TargetContract(**payload["target"])
    return SupervisedDatasetManifest(
        version=str(payload["version"]),
        feature_schema=schema,
        feature_set_version=str(payload["feature_set_version"]),
        feature_artifact_version=str(payload["feature_artifact_version"]),
        label_version=str(payload["label_version"]),
        label_artifact_version=str(payload["label_artifact_version"]),
        universe_version=str(payload["universe_version"]),
        dataset_version=str(payload["dataset_version"]),
        availability_policy_version=str(payload["availability_policy_version"]),
        target=target,
        missing_feature_policy=MissingFeaturePolicy(payload["missing_feature_policy"]),
        universe_size_by_date=tuple(
            (str(day), int(count)) for day, count in payload["universe_size_by_date"]
        ),
        valid_sample_count_by_date=tuple(
            (str(day), int(count)) for day, count in payload["valid_sample_count_by_date"]
        ),
        checksum=str(payload["checksum"]),
    )


class SupervisedDatasetBuilder:
    def build(
        self,
        features: pl.DataFrame,
        labels: pl.DataFrame,
        universe: pl.DataFrame,
        *,
        version: str,
        feature_names: tuple[str, ...],
        feature_set_version: str,
        feature_artifact_version: str,
        label_artifact_version: str,
        universe_version: str,
        dataset_version: str,
        availability_policy_version: str,
        target: TargetContract,
        missing_feature_policy: MissingFeaturePolicy,
    ) -> SupervisedDataset:
        versions = {
            version,
            feature_artifact_version,
            label_artifact_version,
            universe_version,
            dataset_version,
        }
        if versions & {"", "latest", "current"}:
            raise ValueError("supervised datasets require explicit pinned artifact versions")
        if not feature_names or len(feature_names) != len(set(feature_names)):
            raise ValueError("feature names must be non-empty and unique")
        forbidden = {"label", "label_value", "target", "raw_return", "benchmark_return"}
        if set(feature_names) & forbidden:
            raise ValueError("label columns cannot enter the feature matrix")
        metadata_columns = {
            "instrument_id",
            "decision_time",
            "decision_date",
            "market",
            "valid",
            "invalid_reason",
        }
        if set(feature_names) & metadata_columns:
            raise ValueError("metadata columns cannot enter the feature matrix")
        duplicate_features = (
            features.group_by(["instrument_id", "decision_time", "feature_name"])
            .len()
            .filter(pl.col("len") > 1)
        )
        if duplicate_features.height:
            raise ValueError("duplicate feature observation")
        duplicate_labels = (
            labels.group_by(["instrument_id", "decision_time"])
            .len()
            .filter(pl.col("len") > 1)
        )
        if duplicate_labels.height:
            raise ValueError("duplicate supervised sample label")
        if "label_version" in labels.columns and set(labels["label_version"].unique()) != {
            target.label_version
        }:
            raise ValueError("label rows do not match the pinned target label version")
        feature_map = {
            (str(row["instrument_id"]), row["decision_time"], str(row["feature_name"])): row
            for row in features.to_dicts()
        }
        label_rows = labels.sort(["decision_time", "instrument_id"]).to_dicts()
        eligible_keys = {
            (str(row["instrument_id"]), row["trade_date"])
            for row in universe.filter(pl.col("eligible")).to_dicts()
        }
        rows: list[dict[str, Any]] = []
        for label in label_rows:
            instrument_id = str(label["instrument_id"])
            decision_time: datetime = label["decision_time"]
            if not isinstance(decision_time, datetime) or decision_time.tzinfo is None:
                raise ValueError("decision_time must be a timezone-aware timestamp")
            if (instrument_id, decision_time.date()) not in eligible_keys:
                continue
            values: list[float | None] = []
            reasons: list[str] = []
            for name in feature_names:
                item = feature_map.get((instrument_id, decision_time, name))
                if item is None or not bool(item["valid"]) or item["value"] is None:
                    values.append(None)
                    reasons.append(f"missing_or_invalid_feature:{name}")
                else:
                    values.append(float(item["value"]))
            if not bool(label["valid"]):
                reasons.append(f"invalid_label:{label.get('invalid_reason')}")
            interval_start, interval_end = _label_interval(label)
            record = {
                "instrument_id": instrument_id,
                "decision_time": decision_time,
                "decision_date": decision_time.date(),
                "feature_vector": values,
                "feature_set_version": feature_set_version,
                "feature_artifact_version": feature_artifact_version,
                "label_value": label.get("label_value"),
                "label_version": target.label_version,
                "label_interval_start": interval_start,
                "label_interval_end": interval_end,
                "universe_version": universe_version,
                "dataset_version": dataset_version,
                "availability_policy_version": availability_policy_version,
                "market": label["market"],
                "valid": not reasons,
                "invalid_reason": ";".join(reasons) if reasons else None,
            }
            if missing_feature_policy is MissingFeaturePolicy.DROP_SAMPLE and reasons:
                continue
            rows.append(record)
        frame = pl.DataFrame(rows, infer_schema_length=None)
        duplicates = (
            frame.group_by(["instrument_id", "decision_time"])
            .len()
            .filter(pl.col("len") > 1)
            if frame.height
            else pl.DataFrame()
        )
        if duplicates.height:
            raise ValueError("duplicate (instrument_id, decision_time) supervised sample")
        schema = FeatureMatrixSchema(feature_names, tuple("float64" for _ in feature_names))
        universe_sizes = tuple(
            (str(row["trade_date"]), int(row["len"]))
            for row in universe.filter(pl.col("eligible"))
            .group_by("trade_date")
            .len()
            .sort("trade_date")
            .to_dicts()
        )
        valid_counts = tuple(
            (str(row["decision_date"]), int(row["len"]))
            for row in frame.filter(pl.col("valid"))
            .group_by("decision_date")
            .len()
            .sort("decision_date")
            .to_dicts()
        )
        payload = {
            "version": version,
            "feature_schema": asdict(schema),
            "feature_set_version": feature_set_version,
            "feature_artifact_version": feature_artifact_version,
            "label_version": target.label_version,
            "label_artifact_version": label_artifact_version,
            "universe_version": universe_version,
            "dataset_version": dataset_version,
            "availability_policy_version": availability_policy_version,
            "target": asdict(target),
            "missing_feature_policy": missing_feature_policy.value,
            "universe_size_by_date": universe_sizes,
            "valid_sample_count_by_date": valid_counts,
            "records": frame.to_dicts(),
        }
        checksum = hashlib.sha256(
            json.dumps(payload, default=str, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        manifest = SupervisedDatasetManifest(
            version,
            schema,
            feature_set_version,
            feature_artifact_version,
            target.label_version,
            label_artifact_version,
            universe_version,
            dataset_version,
            availability_policy_version,
            target,
            missing_feature_policy,
            universe_sizes,
            valid_counts,
            checksum,
        )
        return SupervisedDataset(frame, manifest)


def _label_interval(
    label: dict[str, Any],
) -> tuple[datetime | None, datetime | None]:
    start = label.get("label_interval_start")
    end = label.get("label_interval_end")
    if not bool(label.get("valid")) and (start is None or end is None):
        return None, None
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        raise ValueError("labels require exact label_interval_start and label_interval_end")
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise ValueError("label intervals must be ordered timezone-aware timestamps")
    return start, end
