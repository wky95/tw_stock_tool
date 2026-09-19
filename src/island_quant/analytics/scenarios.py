"""Deterministic, isolated sensitivity scenarios without performance selection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ScenarioConfig:
    version: str
    slippage_bps: Decimal
    participation_cap: Decimal
    fee_multiplier: Decimal
    tax_multiplier: Decimal
    rebalance_frequency: str
    rebalance_interval_sessions: int
    top_k: int

    @property
    def checksum(self) -> str:
        return hashlib.sha256(_canonical(asdict(self))).hexdigest()

    @property
    def scenario_id(self) -> str:
        return self.checksum


@dataclass(frozen=True, slots=True)
class ScenarioOutcome:
    config: ScenarioConfig
    status: str
    total_return: Decimal | None
    turnover: Decimal | None
    slippage_cost: Decimal | None
    capacity_proxy: Decimal | None
    failure_reason_code: str | None
    error: str | None
    checksum: str


class ScenarioRunner:
    version = "isolated-sensitivity-grid-v1"

    def __init__(self, maximum_scenarios: int = 1_000) -> None:
        if maximum_scenarios < 1:
            raise ValueError("maximum scenario count must be positive")
        self.maximum_scenarios = maximum_scenarios

    def run(
        self,
        scenarios: tuple[ScenarioConfig, ...],
        evaluator: Callable[[ScenarioConfig], tuple[Decimal, Decimal, Decimal, Decimal]],
        *,
        allow_large_grid: bool = False,
    ) -> tuple[ScenarioOutcome, ...]:
        if len(scenarios) > self.maximum_scenarios and not allow_large_grid:
            raise ValueError("scenario grid exceeds the configured maximum")
        identities = [config.scenario_id for config in scenarios]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate scenario configuration")
        outcomes: list[ScenarioOutcome] = []
        for config in sorted(scenarios, key=lambda item: item.scenario_id):
            total_return: Decimal | None
            turnover: Decimal | None
            slippage: Decimal | None
            capacity: Decimal | None
            try:
                total_return, turnover, slippage, capacity = evaluator(config)
                status = "completed"
                failure_reason_code = None
                error = None
            except Exception as exc:
                total_return, turnover, slippage, capacity = None, None, None, None
                status = "failed"
                failure_reason_code = type(exc).__name__
                error = "scenario evaluation failed"
            payload = {
                "runner_version": self.version,
                "config_checksum": config.checksum,
                "status": status,
                "total_return": total_return,
                "turnover": turnover,
                "slippage": slippage,
                "capacity": capacity,
                "error": error,
                "failure_reason_code": failure_reason_code,
            }
            outcomes.append(
                ScenarioOutcome(
                    config,
                    status,
                    total_return,
                    turnover,
                    slippage,
                    capacity,
                    failure_reason_code,
                    error,
                    hashlib.sha256(_canonical(payload)).hexdigest(),
                )
            )
        return tuple(outcomes)


def default_scenario_grid() -> tuple[ScenarioConfig, ...]:
    rows: list[ScenarioConfig] = []
    for slippage in (0, 5, 10, 25, 50):
        for participation in ("0.01", "0.05", "0.10", "0.25"):
            for fee in ("0.5", "1", "2"):
                for frequency, interval in (("daily", 1), ("weekly", 1), ("n_sessions", 3)):
                    for top_k in (1, 2, 3):
                        rows.append(
                            ScenarioConfig(
                                "sensitivity-grid-v1",
                                Decimal(slippage),
                                Decimal(participation),
                                Decimal(fee),
                                Decimal("1"),
                                frequency,
                                interval,
                                top_k,
                            )
                        )
    return tuple(rows)


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
