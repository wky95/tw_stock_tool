"""Versioned, provider-neutral feature contracts and deterministic registry."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class MissingValuePolicy(StrEnum):
    INVALID = "invalid"
    PRESERVE = "preserve"


class FeatureRegistryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FeatureContract:
    name: str
    version: str
    mathematical_definition: str
    required_input_columns: tuple[str, ...]
    required_price_view: str
    lookback_sessions: int
    minimum_observations: int
    decision_time_rule: str
    availability_policy_version: str
    missing_value_policy: MissingValuePolicy
    winsorization_policy: str
    normalization_policy: str
    neutralization_policy: str
    expected_direction: str | None
    supported_markets: tuple[str, ...]
    output_dtype: str
    output_unit: str
    code_version: str
    dataset_version: str

    def __post_init__(self) -> None:
        if not self.name or not self.version or not self.mathematical_definition:
            raise ValueError("feature identity and mathematical definition are required")
        if self.lookback_sessions < 1 or self.minimum_observations < 1:
            raise ValueError("feature lookback and minimum observations must be positive")
        if self.minimum_observations > self.lookback_sessions + 1:
            raise ValueError("minimum observations exceeds the declared lookback")
        if self.required_price_view != "canonical_unadjusted":
            raise ValueError("baseline factors require canonical_unadjusted prices")
        if not self.required_input_columns:
            raise ValueError("feature must declare input columns")

    @property
    def identity(self) -> str:
        encoded = json.dumps(
            self.to_manifest(), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def to_manifest(self) -> dict[str, Any]:
        manifest = asdict(self)
        manifest["missing_value_policy"] = self.missing_value_policy.value
        return manifest


@dataclass(frozen=True, slots=True)
class FeatureSetManifest:
    version: str
    dataset_version: str
    contracts: tuple[dict[str, Any], ...]
    dependencies: dict[str, tuple[str, ...]]
    checksum: str


class FeatureRegistry:
    def __init__(self) -> None:
        self._contracts: dict[tuple[str, str], FeatureContract] = {}
        self._dependencies: dict[tuple[str, str], tuple[str, ...]] = {}

    def register(
        self, contract: FeatureContract, dependencies: tuple[str, ...] = ()
    ) -> None:
        key = (contract.name, contract.version)
        existing = self._contracts.get(key)
        if existing is not None and existing.identity != contract.identity:
            raise FeatureRegistryError(
                f"feature {contract.name}@{contract.version} has a conflicting definition"
            )
        self._contracts[key] = contract
        self._dependencies[key] = tuple(sorted(dependencies))

    def get(self, name: str, version: str) -> FeatureContract:
        try:
            return self._contracts[(name, version)]
        except KeyError as exc:
            raise FeatureRegistryError(f"unknown feature: {name}@{version}") from exc

    def list(self) -> tuple[FeatureContract, ...]:
        return tuple(self._contracts[key] for key in sorted(self._contracts))

    def dependencies(self, name: str, version: str) -> tuple[str, ...]:
        self.get(name, version)
        return self._dependencies[(name, version)]

    def build_feature_set(
        self, version: str, features: tuple[tuple[str, str], ...], dataset_version: str
    ) -> FeatureSetManifest:
        if not version or not dataset_version or dataset_version in {"latest", "current"}:
            raise FeatureRegistryError("feature sets require explicit versions")
        contracts = tuple(
            self.get(name, feature_version).to_manifest()
            for name, feature_version in sorted(set(features))
        )
        dependencies = {
            f"{name}@{feature_version}": self.dependencies(name, feature_version)
            for name, feature_version in sorted(set(features))
        }
        payload = {
            "version": version,
            "dataset_version": dataset_version,
            "contracts": contracts,
            "dependencies": dependencies,
        }
        checksum = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return FeatureSetManifest(version, dataset_version, contracts, dependencies, checksum)
