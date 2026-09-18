"""Typed application configuration with safe trading defaults."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import time, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PAPER = "paper"
    LIVE = "live"


class TradingMode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class ResearchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    random_seed: int = 42
    data_root: Path = Path("data")
    artifact_root: Path = Path("artifacts")


class DataSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "finmind"
    raw_root: Path = Path("data/raw")
    normalized_root: Path = Path("data/normalized")
    checkpoint_root: Path = Path("state/ingestion")
    catalog_path: Path = Path("state/catalog.duckdb")
    schema_version: int = Field(default=1, gt=0)
    request_attempts: int = Field(default=4, gt=0, le=10)
    request_timeout_seconds: float = Field(default=30, gt=0, le=120)


class UniverseSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_version: str = "tw-equity-v1"
    minimum_listing_days: int = Field(default=60, ge=0)
    trailing_median_window: int = Field(default=20, gt=0)
    minimum_trailing_median_traded_value: Decimal = Field(
        default=Decimal("5000000"), ge=0
    )
    minimum_lookback_observations: int = Field(default=20, gt=0)


class AvailabilitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_version: str = "tw-daily-v1"
    publication_time: time = time(17, 30)
    finalization_buffer_minutes: int = Field(default=30, ge=0, le=1440)

    @property
    def finalization_buffer(self) -> timedelta:
        return timedelta(minutes=self.finalization_buffer_minutes)


class DatasetVersionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transformation_code_version: str = "phase1-slice2-v1"
    configuration_version: str = "default-v1"
    corporate_action_schema_version: int = Field(default=1, gt=0)
    label_schema_version: int = Field(default=1, gt=0)


class TradingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: TradingMode = TradingMode.BACKTEST
    live_enabled: bool = False
    allow_short: bool = False
    initial_cash: Decimal = Field(default=Decimal("1000000"), gt=0)
    maximum_gross_exposure: Decimal = Field(default=Decimal("1"), gt=0)
    maximum_net_exposure: Decimal = Field(default=Decimal("1"), ge=0)
    maximum_position_weight: Decimal = Field(default=Decimal("0.10"), gt=0, le=1)

    @model_validator(mode="after")
    def live_requires_explicit_gate(self) -> TradingSettings:
        if self.mode is TradingMode.LIVE and not self.live_enabled:
            raise ValueError("live mode requires trading.live_enabled=true")
        if self.mode is not TradingMode.LIVE and self.live_enabled:
            raise ValueError("trading.live_enabled may only be true in live mode")
        return self


class LoggingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    level: str = "INFO"
    json_output: bool = Field(default=True, alias="json")


class AppSettings(BaseSettings):
    """Root configuration. Unknown keys fail fast to catch unsafe typos."""

    model_config = SettingsConfigDict(extra="forbid", validate_default=True)

    environment: Environment = Environment.DEVELOPMENT
    timezone: str = "Asia/Taipei"
    base_currency: str = "TWD"
    research: ResearchSettings = Field(default_factory=ResearchSettings)
    data: DataSettings = Field(default_factory=DataSettings)
    universe: UniverseSettings = Field(default_factory=UniverseSettings)
    availability: AvailabilitySettings = Field(default_factory=AvailabilitySettings)
    dataset_versions: DatasetVersionSettings = Field(default_factory=DatasetVersionSettings)
    trading: TradingSettings = Field(default_factory=TradingSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    @model_validator(mode="after")
    def live_environment_matches_mode(self) -> AppSettings:
        if self.trading.mode is TradingMode.LIVE and self.environment is not Environment.LIVE:
            raise ValueError("live trading requires environment=live")
        if self.environment is Environment.LIVE and self.trading.mode is not TradingMode.LIVE:
            raise ValueError("environment=live requires trading.mode=live")
        return self

    def safe_dump(self) -> dict[str, Any]:
        """Return a JSON-compatible, non-secret configuration representation."""
        return self.model_dump(mode="json", by_alias=True)


def _deep_merge(target: dict[str, Any], path: list[str], value: Any) -> None:
    current = target
    for part in path[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError(f"environment override conflicts at {part}")
        current = child
    current[path[-1]] = value


def _environment_overrides(prefix: str = "ISLAND_QUANT__") -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for key, raw_value in os.environ.items():
        if not key.startswith(prefix):
            continue
        path = [part.lower() for part in key[len(prefix) :].split("__") if part]
        if not path:
            continue
        _deep_merge(overrides, path, yaml.safe_load(raw_value))
    return overrides


def _merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _merge(dict(result[key]), value)
        else:
            result[key] = value
    return result


def load_settings(path: str | Path) -> AppSettings:
    """Load YAML and apply ``ISLAND_QUANT__SECTION__KEY`` overrides."""
    config_path = Path(path)
    with config_path.open(encoding="utf-8") as stream:
        loaded = yaml.safe_load(stream) or {}
    if not isinstance(loaded, dict):
        raise ValueError("configuration root must be a mapping")
    return AppSettings.model_validate(_merge(loaded, _environment_overrides()))
