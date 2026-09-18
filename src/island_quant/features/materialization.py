"""Feature candidate storage, promotion gates, and row-level lineage inspection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import polars as pl

from island_quant.research.completeness import ResearchCompleteness
from island_quant.storage.ports import ArtifactStore, DatasetArtifact


@dataclass(frozen=True, slots=True)
class FeatureCandidate:
    artifact: DatasetArtifact
    dataset_name: str
    feature_set_version: str
    completeness: ResearchCompleteness


class FeatureMaterializer:
    def __init__(self, store: ArtifactStore) -> None:
        self.store = store

    def stage(
        self,
        frame: pl.DataFrame,
        *,
        feature_set_version: str,
        input_dataset_version: str,
        universe_version: str,
        label_version: str,
        availability_policy_version: str,
        completeness: ResearchCompleteness,
        feature_set_checksum: str,
        schema_version: int = 1,
    ) -> FeatureCandidate:
        dataset = f"features__{feature_set_version}"
        artifact = self.store.stage_dataset(
            dataset,
            frame.sort(["decision_date", "feature_name", "instrument_id"]),
            {
                "schema_version": schema_version,
                "source_checksums": [
                    input_dataset_version,
                    universe_version,
                    label_version,
                    availability_policy_version,
                ],
                "transformation_code_version": "feature-engine-v1",
                "transformation_algorithm_version": feature_set_version,
                "configuration_version": feature_set_checksum,
                "configuration": {
                    "feature_set_checksum": feature_set_checksum,
                    "research_quality": completeness.classification,
                    "dataset_version": input_dataset_version,
                    "universe_version": universe_version,
                    "label_version": label_version,
                    "availability_policy_version": availability_policy_version,
                    "code_version": "phase1-slice3-v1",
                },
                "quality_flags": (
                    []
                    if completeness.classification == "validated"
                    else ["INCOMPLETE_POINT_IN_TIME_REFERENCE_DATA"]
                ),
            },
        )
        return FeatureCandidate(artifact, dataset, feature_set_version, completeness)

    def promote(
        self, candidate: FeatureCandidate, *, validated: bool
    ) -> DatasetArtifact:
        if validated:
            candidate.completeness.require_validated()
        return self.store.promote_dataset(
            candidate.dataset_name,
            candidate.artifact.checksum,
            validated=validated,
        )

    @staticmethod
    def inspect(
        frame: pl.DataFrame, instrument_id: str, decision_date: object, feature_name: str
    ) -> dict[str, Any]:
        selected = frame.filter(
            (pl.col("instrument_id") == instrument_id)
            & (pl.col("decision_date") == decision_date)
            & (pl.col("feature_name") == feature_name)
        )
        if selected.height != 1:
            raise ValueError("feature lineage lookup must resolve exactly one row")
        return selected.row(0, named=True)
