"""Exact-version paper target ingestion and conservative order-intent conversion."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

from island_quant.domain.models import Market
from island_quant.oms.models import OMSState, PaperOrderLineage
from island_quant.oms.repository import SQLiteOMSRepository
from island_quant.oms.service import PaperOMSService, PaperOrderCommand
from island_quant.pipeline.artifacts import ExactArtifactStore, require_exact_version

ZERO = Decimal("0")
REQUIRED_LINEAGE = frozenset(
    {
        "strategy_version",
        "model_artifact_version",
        "prediction_artifact_version",
        "dataset_version",
        "universe_version",
        "target_policy_version",
        "instrument_reference_version",
        "calendar_version",
    }
)


@dataclass(frozen=True, slots=True)
class PaperTarget:
    instrument_id: str
    market: Market
    execution_session: date
    decision_time: datetime
    available_at: datetime
    target_weight: Decimal
    reference_price: Decimal
    eligible: bool

    @property
    def instrument_key(self) -> str:
        return f"{self.market.value}:{self.instrument_id}"


@dataclass(frozen=True, slots=True)
class PaperTargetSnapshot:
    artifact_version: str
    lineage: tuple[tuple[str, str], ...]
    targets: tuple[PaperTarget, ...]

    def lineage_value(self, name: str) -> str:
        values = dict(self.lineage)
        return values[name]


@dataclass(frozen=True, slots=True)
class PaperStrategyRiskPolicy:
    version: str = "paper-target-risk-v1"
    initial_cash: Decimal = Decimal("1000000")
    maximum_gross_exposure: Decimal = Decimal("1")
    maximum_single_position: Decimal = Decimal("0.20")
    maximum_orders_per_session: int = 20
    estimated_adverse_slippage_bps: Decimal = Decimal("5")

    def __post_init__(self) -> None:
        if self.initial_cash <= ZERO:
            raise ValueError("paper strategy initial cash must be positive")
        if not ZERO < self.maximum_gross_exposure <= Decimal("1"):
            raise ValueError("paper gross exposure limit must be in (0, 1]")
        if not ZERO < self.maximum_single_position <= Decimal("1"):
            raise ValueError("paper single-position limit must be in (0, 1]")
        if self.maximum_orders_per_session < 1:
            raise ValueError("paper order count limit must be positive")
        if self.estimated_adverse_slippage_bps < ZERO:
            raise ValueError("paper estimated slippage cannot be negative")


class ExactPaperTargetReader:
    """Reads only validated paper-candidate target snapshots by exact CAS version."""

    artifact_type = "paper_target_snapshots"
    schema_version = 1

    def __init__(self, root: Path) -> None:
        self.store = ExactArtifactStore(root, self.artifact_type, self.schema_version)

    def read(
        self,
        version: str,
        *,
        session: date,
        as_of: datetime,
    ) -> PaperTargetSnapshot:
        require_exact_version(version)
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("paper target read timestamp must be timezone-aware")
        manifest = self.store.manifest(version)
        if manifest.created_at.tzinfo is None or manifest.created_at.utcoffset() is None:
            raise RuntimeError("paper target artifact creation timestamp must be timezone-aware")
        if manifest.created_at > as_of:
            raise RuntimeError("paper target artifact was not available at execution time")
        if manifest.completeness != "validated" or manifest.classification != "paper_candidate":
            raise RuntimeError("paper target artifact is not a validated paper candidate")
        lineage = dict(manifest.lineage)
        missing = REQUIRED_LINEAGE - lineage.keys()
        if missing:
            raise RuntimeError(f"paper target lineage is incomplete: {','.join(sorted(missing))}")
        targets: list[PaperTarget] = []
        seen: set[str] = set()
        for batch in self.store.batches(version):
            for raw in batch.records:
                target = self._target(raw)
                if target.execution_session != session:
                    raise RuntimeError("paper target execution session mismatch")
                if target.decision_time.date() >= target.execution_session:
                    raise RuntimeError("paper target must execute after its decision session")
                if target.available_at > target.decision_time or target.decision_time > as_of:
                    raise RuntimeError("paper target violates point-in-time availability")
                if not target.eligible:
                    raise RuntimeError(
                        "ineligible instrument is forbidden in paper target artifact"
                    )
                if target.instrument_key in seen:
                    raise RuntimeError("duplicate instrument in paper target artifact")
                seen.add(target.instrument_key)
                targets.append(target)
        if not targets:
            raise RuntimeError("paper target artifact contains no targets")
        if len({item.decision_time for item in targets}) != 1:
            raise RuntimeError("paper target artifact mixes decision times")
        total = sum((item.target_weight for item in targets), ZERO)
        if total > Decimal("1"):
            raise RuntimeError("paper target weights exceed long-only capital")
        return PaperTargetSnapshot(version, manifest.lineage, tuple(sorted(targets, key=_key)))

    @staticmethod
    def _target(raw: dict[str, object]) -> PaperTarget:
        try:
            eligible = raw["eligible"]
            if not isinstance(eligible, bool):
                raise TypeError("eligible must be a boolean")
            decision_time = datetime.fromisoformat(str(raw["decision_time"]))
            available_at = datetime.fromisoformat(str(raw["available_at"]))
            target = PaperTarget(
                str(raw["instrument_id"]),
                Market(str(raw["market"])),
                date.fromisoformat(str(raw["execution_session"])),
                decision_time,
                available_at,
                Decimal(str(raw["target_weight"])),
                Decimal(str(raw["reference_price"])),
                eligible,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("paper target record is invalid") from exc
        if decision_time.tzinfo is None or available_at.tzinfo is None:
            raise RuntimeError("paper target timestamps must be timezone-aware")
        if not ZERO <= target.target_weight <= Decimal("1"):
            raise RuntimeError("paper targets must be long-only weights in [0, 1]")
        if target.reference_price <= ZERO:
            raise RuntimeError("paper target reference price must be positive")
        return target


class PaperTargetExecutor:
    """Converts one validated snapshot into idempotent persistent OMS commands."""

    def __init__(
        self,
        oms: SQLiteOMSRepository,
        service: PaperOMSService,
        instrument_markets: dict[str, Market],
        execution_prices: dict[str, Decimal],
        policy: PaperStrategyRiskPolicy | None = None,
    ) -> None:
        self.oms = oms
        self.service = service
        self.instrument_markets = instrument_markets
        self.execution_prices = execution_prices
        self.policy = policy or PaperStrategyRiskPolicy()

    def execute(
        self,
        snapshot: PaperTargetSnapshot,
        portfolio: dict[str, object] | None,
        *,
        created_at: datetime,
    ) -> tuple[str, ...]:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("paper target execution timestamp must be timezone-aware")
        gross = sum((item.target_weight for item in snapshot.targets), ZERO)
        if gross > self.policy.maximum_gross_exposure:
            raise RuntimeError("paper target gross exposure limit exceeded")
        if any(
            item.target_weight > self.policy.maximum_single_position
            for item in snapshot.targets
        ):
            raise RuntimeError("paper target single-position limit exceeded")
        nav, available_cash, positions = self._portfolio(portfolio)
        target_keys = {item.instrument_key for item in snapshot.targets}
        missing_targets = positions.keys() - target_keys
        if missing_targets:
            raise RuntimeError("paper target snapshot omits an existing position")
        projected = dict(positions)
        for order in self.oms.list_orders():
            if order.state in _OPEN_ORDER_STATES:
                remaining = order.quantity - order.filled_quantity
                key = self._position_key(order.instrument_id, snapshot.targets)
                projected[key] = projected.get(key, 0) + (
                    remaining if order.side == "buy" else -remaining
                )
        desired_positions: dict[str, int] = {}
        commands: list[PaperOrderCommand] = []
        for target in snapshot.targets:
            if self.instrument_markets.get(target.instrument_id) is not target.market:
                raise RuntimeError("paper target does not match pinned instrument reference")
            if self.execution_prices.get(target.instrument_id) != target.reference_price:
                raise RuntimeError("paper target does not match pinned execution price")
            desired = int(
                (nav * target.target_weight / target.reference_price).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )
            desired_positions[target.instrument_key] = desired
            delta = desired - projected.get(target.instrument_key, 0)
            if delta == 0:
                continue
            side = "buy" if delta > 0 else "sell"
            quantity = abs(delta)
            identity = {
                "artifact_version": snapshot.artifact_version,
                "execution_session": target.execution_session.isoformat(),
                "instrument": target.instrument_key,
                "side": side,
                "quantity": quantity,
                "risk_policy_version": self.policy.version,
            }
            key = hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            lineage = PaperOrderLineage(
                snapshot.artifact_version,
                snapshot.lineage_value("strategy_version"),
                snapshot.lineage_value("model_artifact_version"),
                snapshot.lineage_value("prediction_artifact_version"),
                snapshot.lineage_value("dataset_version"),
                snapshot.lineage_value("universe_version"),
                snapshot.lineage_value("target_policy_version"),
                self.policy.version,
                target.execution_session.isoformat(),
                target.decision_time,
                target.target_weight,
            )
            commands.append(
                PaperOrderCommand(
                    f"paper-target-{key[:24]}",
                    key,
                    target.instrument_id,
                    side,
                    quantity,
                    target.reference_price
                    * (
                        Decimal("1")
                        + self.policy.estimated_adverse_slippage_bps / Decimal("10000")
                        if side == "buy"
                        else Decimal("1")
                    ),
                    available_cash,
                    created_at,
                    positions.get(target.instrument_key, 0),
                    lineage,
                )
            )
        projected_gross = sum(
            (
                Decimal(quantity)
                * self.execution_prices[_symbol(key)]
                / nav
                for key, quantity in desired_positions.items()
            ),
            ZERO,
        )
        if projected_gross > self.policy.maximum_gross_exposure:
            raise RuntimeError("projected paper position gross exposure limit exceeded")
        if any(
            Decimal(quantity) * self.execution_prices[_symbol(key)] / nav
            > self.policy.maximum_single_position
            for key, quantity in desired_positions.items()
        ):
            raise RuntimeError("projected paper single-position limit exceeded")
        session_order_count = sum(
            order.created_at.date() == snapshot.targets[0].execution_session
            for order in self.oms.list_orders()
        )
        if session_order_count + len(commands) > self.policy.maximum_orders_per_session:
            raise RuntimeError("paper target order count limit exceeded")
        # Sells reserve inventory first. Their unsettled proceeds never increase buy cash.
        commands.sort(key=lambda item: (item.side != "sell", item.instrument_id))
        return tuple(self.service.queue(command).order_id for command in commands)

    def _portfolio(
        self, portfolio: dict[str, object] | None
    ) -> tuple[Decimal, Decimal, dict[str, int]]:
        if portfolio is None:
            return self.policy.initial_cash, self.policy.initial_cash, {}
        if not bool(portfolio.get("valuation_complete")):
            raise RuntimeError("complete portfolio valuation is required before new paper risk")
        positions: dict[str, int] = {}
        raw_positions = portfolio.get("positions")
        if not isinstance(raw_positions, list):
            raise RuntimeError("paper portfolio positions are malformed")
        for item in raw_positions:
            if not isinstance(item, dict):
                raise RuntimeError("paper portfolio position is malformed")
            positions[str(item["instrument_id"])] = int(str(item["quantity"]))
        return (
            Decimal(str(portfolio["nav"])),
            Decimal(str(portfolio["available_cash"])),
            positions,
        )

    @staticmethod
    def _position_key(instrument_id: str, targets: tuple[PaperTarget, ...]) -> str:
        matches = [item.instrument_key for item in targets if item.instrument_id == instrument_id]
        if len(matches) != 1:
            raise RuntimeError("open order instrument cannot be resolved to target reference")
        return matches[0]


_OPEN_ORDER_STATES = {
    OMSState.RISK_APPROVED,
    OMSState.SUBMIT_PENDING,
    OMSState.SUBMITTED,
    OMSState.ACKNOWLEDGED,
    OMSState.PARTIALLY_FILLED,
    OMSState.CANCEL_PENDING,
    OMSState.REPLACE_PENDING,
    OMSState.UNKNOWN,
}


def _key(target: PaperTarget) -> str:
    return target.instrument_key


def _symbol(instrument_key: str) -> str:
    try:
        _, symbol = instrument_key.split(":", 1)
    except ValueError as exc:
        raise RuntimeError("paper position instrument key is malformed") from exc
    return symbol
