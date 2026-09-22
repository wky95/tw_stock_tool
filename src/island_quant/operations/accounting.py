"""Deterministic OMS-fill to persistent paper-accounting projection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

from island_quant.backtest.policies import FeeTaxPolicy, SettlementPolicy
from island_quant.domain.models import AssetType, Fill, Instrument, Market, Side
from island_quant.oms.repository import SQLiteOMSRepository
from island_quant.operations.monitoring import OperationsStateStore
from island_quant.portfolio.accounting import AccountingSnapshot, PortfolioLedger


@dataclass(frozen=True, slots=True)
class PaperInstrumentReference:
    instrument_id: str
    market: Market
    reference_version: str


@dataclass(frozen=True, slots=True)
class PaperMark:
    instrument_id: str
    session: date
    price: Decimal
    marked_at: datetime
    source: str


@dataclass(frozen=True, slots=True)
class PaperAccountingPolicy:
    version: str = "paper-accounting-projection-v1"
    portfolio_id: str = "paper-primary"
    initial_cash: Decimal = Decimal("1000000")
    fee_policy: FeeTaxPolicy = FeeTaxPolicy()
    settlement_policy: SettlementPolicy = SettlementPolicy()


@dataclass(frozen=True, slots=True)
class PaperProjectionResult:
    projection_version: str
    source_checksum: str
    snapshot: AccountingSnapshot
    applied_fill_ids: tuple[str, ...]


class PaperAccountingProjector:
    def __init__(
        self,
        oms: SQLiteOMSRepository,
        state: OperationsStateStore,
        policy: PaperAccountingPolicy,
        instruments: tuple[PaperInstrumentReference, ...],
        trading_sessions: tuple[date, ...],
        marks: tuple[PaperMark, ...] = (),
    ) -> None:
        self.oms = oms
        self.state = state
        self.policy = policy
        self.instruments = {item.instrument_id: item for item in instruments}
        self.trading_sessions = trading_sessions
        self.marks = {(item.instrument_id, item.session): item for item in marks}

    def project(self, as_of: datetime) -> PaperProjectionResult:
        if as_of.tzinfo is None:
            raise ValueError("paper projection timestamp must be timezone-aware")
        rows = tuple(
            row
            for row in self.oms.fills_with_orders()
            if datetime.fromisoformat(str(row["event_time"])) <= as_of
        )
        source_checksum = _checksum(rows)
        opened_at = datetime.fromisoformat(str(rows[0]["event_time"])) if rows else as_of
        ledger = PortfolioLedger(
            self.policy.portfolio_id,
            self.policy.initial_cash,
            self.policy.settlement_policy,
            opened_at,
        )
        cumulative_gross: dict[str, Decimal] = {}
        cumulative_fee: dict[str, Decimal] = {}
        applied: list[str] = []
        for session in self.trading_sessions:
            if session <= as_of.date():
                ledger.process_settlements(session, _at_session(session, as_of))
                marked_keys: set[str] = set()
                for row in rows:
                    event_time = datetime.fromisoformat(str(row["event_time"]))
                    if event_time.date() != session:
                        continue
                    symbol = str(row["instrument_id"])
                    reference = self.instruments.get(symbol)
                    if reference is None:
                        raise RuntimeError(f"missing pinned instrument reference: {symbol}")
                    side = Side(str(row["side"]))
                    quantity = int(row["quantity"])
                    price = Decimal(str(row["price"]))
                    order_id = str(row["order_id"])
                    gross = price * quantity
                    fee, tax = self.policy.fee_policy.incremental_costs(
                        side,
                        cumulative_gross.get(order_id, Decimal("0")),
                        gross,
                        cumulative_fee.get(order_id, Decimal("0")),
                    )
                    cumulative_gross[order_id] = (
                        cumulative_gross.get(order_id, Decimal("0")) + gross
                    )
                    cumulative_fee[order_id] = cumulative_fee.get(order_id, Decimal("0")) + fee
                    instrument = Instrument(symbol, reference.market, AssetType.EQUITY, "TWD", 1)
                    fill = Fill(
                        str(row["fill_id"]),
                        str(row["client_order_id"]),
                        instrument,
                        side,
                        quantity,
                        price,
                        fee,
                        tax,
                        event_time,
                        event_time,
                    )
                    ledger.apply_fill(fill, session, self.trading_sessions)
                    applied.append(fill.fill_id)
                    marked_keys.add(instrument.key)
                for symbol, reference in self.instruments.items():
                    mark = self.marks.get((symbol, session))
                    if mark is None:
                        continue
                    instrument = Instrument(symbol, reference.market, AssetType.EQUITY, "TWD", 1)
                    ledger.mark(instrument, mark.price, mark.marked_at, mark.source)
                    marked_keys.add(instrument.key)
                ledger.age_unmarked_positions(marked_keys)
        if len(applied) != len(rows):
            raise RuntimeError("OMS fill is outside the pinned trading calendar")
        snapshot = ledger.snapshot(as_of)
        prior = self.state.prior_portfolios(as_of)
        prior_nav = Decimal(str(prior[-1]["nav"])) if prior else self.policy.initial_cash
        peak_nav = max(
            (self.policy.initial_cash, snapshot.net_asset_value),
            default=self.policy.initial_cash,
        )
        if prior:
            peak_nav = max(
                peak_nav,
                *(Decimal(str(item["nav"])) for item in prior),
            )
        document = _snapshot_document(
            snapshot,
            self.policy,
            source_checksum,
            tuple(applied),
            tuple(sorted((key, item.reference_version) for key, item in self.instruments.items())),
            _checksum(tuple({"session": item.isoformat()} for item in self.trading_sessions)),
            prior_nav,
            peak_nav,
            tuple(str(item["projection_version"]) for item in prior),
        )
        projection_version = hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.state.publish_portfolio(
            projection_version,
            source_checksum,
            snapshot.checksum,
            document,
            as_of,
        )
        return PaperProjectionResult(projection_version, source_checksum, snapshot, tuple(applied))


def _snapshot_document(
    snapshot: AccountingSnapshot,
    policy: PaperAccountingPolicy,
    source_checksum: str,
    fills: tuple[str, ...],
    reference_versions: tuple[tuple[str, str], ...],
    calendar_checksum: str,
    prior_nav: Decimal,
    peak_nav: Decimal,
    prior_projection_versions: tuple[str, ...],
) -> dict[str, object]:
    market_value = snapshot.positions_market_value
    nav = snapshot.net_asset_value
    gross = market_value / nav if nav else Decimal("0")
    drawdown = (peak_nav - nav) / peak_nav if peak_nav and nav < peak_nav else Decimal("0")
    return {
        "schema_version": 1,
        "environment": "paper",
        "policy_version": policy.version,
        "fee_policy_version": policy.fee_policy.version,
        "settlement_policy_version": policy.settlement_policy.version,
        "as_of": snapshot.as_of.isoformat(),
        "source_checksum": source_checksum,
        "instrument_reference_versions": reference_versions,
        "calendar_checksum": calendar_checksum,
        "prior_projection_versions": prior_projection_versions,
        "snapshot_checksum": snapshot.checksum,
        "applied_fill_ids": fills,
        "cash": str(snapshot.settled_cash),
        "available_cash": str(snapshot.available_cash),
        "settlement_receivables": str(snapshot.settlement_receivables),
        "settlement_payables": str(snapshot.settlement_payables),
        "nav": str(nav),
        "daily_pnl": str(nav - prior_nav),
        "drawdown": str(drawdown),
        "drawdown_basis": "initial_cash_and_persisted_prior_snapshots",
        "gross_exposure": str(gross),
        "net_exposure": str(gross),
        "fees": str(snapshot.fees),
        "taxes": str(snapshot.taxes),
        "positions": [
            {"instrument_id": key, "quantity": quantity}
            for key, quantity in snapshot.position_quantities
        ],
        "marks": [
            [key, str(price), marked_at, source, stale, stale_sessions]
            for key, price, marked_at, source, stale, stale_sessions in snapshot.position_marks
        ],
        "valuation_complete": snapshot.valuation_complete,
        "reconciliation_residual": str(snapshot.reconciliation_residual),
    }


def _checksum(rows: object) -> str:
    return hashlib.sha256(
        json.dumps(rows, default=str, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _at_session(session: date, as_of: datetime) -> datetime:
    return datetime.combine(session, time.min, tzinfo=as_of.tzinfo)
