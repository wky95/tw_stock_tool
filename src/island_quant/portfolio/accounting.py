"""Fail-closed TWD cash-account ledger with double-entry journal postings."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from island_quant.backtest.policies import MarkPolicy, SettlementPolicy
from island_quant.domain.models import Fill, Instrument, Side

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class Posting:
    account: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class AccountingTransaction:
    transaction_id: str
    occurred_at: datetime
    kind: str
    reference_id: str
    postings: tuple[Posting, ...]
    metadata: dict[str, str]

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("accounting transaction time must be timezone-aware")
        if sum((posting.amount for posting in self.postings), ZERO) != ZERO:
            raise ValueError("accounting transaction postings must balance to zero")


@dataclass(frozen=True, slots=True)
class PendingSettlement:
    settlement_id: str
    due_date: date
    side: Side
    amount: Decimal
    settled: bool = False


@dataclass(frozen=True, slots=True)
class CashDividend:
    action_id: str
    instrument: Instrument
    ex_date: date
    record_date: date
    pay_date: date
    amount_per_share: Decimal
    announcement_at: datetime
    action_version: str
    policy_version: str = "cash-dividend-accounting-v1"

    def __post_init__(self) -> None:
        if (
            not self.action_id
            or not self.policy_version
            or not self.action_version
            or self.amount_per_share < ZERO
        ):
            raise ValueError("cash dividend contract is invalid")
        if self.announcement_at.tzinfo is None or self.announcement_at.utcoffset() is None:
            raise ValueError("dividend announcement must be timezone-aware")
        if self.announcement_at.date() > self.ex_date:
            raise ValueError("dividend must be announced no later than ex-date")
        if not self.ex_date <= self.record_date <= self.pay_date:
            raise ValueError("dividend ex/record/payment dates are inconsistent")


@dataclass(frozen=True, slots=True)
class StockSplit:
    action_id: str
    instrument: Instrument
    effective_date: date
    ratio: Decimal
    announcement_at: datetime
    action_version: str
    policy_version: str = "stock-split-accounting-v1"

    def __post_init__(self) -> None:
        if (
            not self.action_id
            or not self.policy_version
            or not self.action_version
            or self.ratio <= ZERO
        ):
            raise ValueError("stock split contract is invalid")
        if self.announcement_at.tzinfo is None or self.announcement_at.utcoffset() is None:
            raise ValueError("split announcement must be timezone-aware")
        if self.announcement_at.date() > self.effective_date:
            raise ValueError("split must be announced before its effective date")


@dataclass(slots=True)
class PositionState:
    instrument: Instrument
    quantity: int = 0
    average_cost: Decimal = ZERO
    market_price: Decimal = ZERO
    book_cost: Decimal = ZERO
    mark_timestamp: datetime | None = None
    mark_source: str | None = None
    stale_sessions: int = 0

    @property
    def cost_basis(self) -> Decimal:
        return self.book_cost

    @property
    def market_value(self) -> Decimal:
        return self.market_price * self.quantity

    @property
    def unrealized_pnl(self) -> Decimal:
        return self.market_value - self.book_cost


@dataclass(frozen=True, slots=True)
class AccountingSnapshot:
    as_of: datetime
    settled_cash: Decimal
    available_cash: Decimal
    settlement_receivables: Decimal
    settlement_payables: Decimal
    dividend_receivables: Decimal
    positions_market_value: Decimal
    net_asset_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal
    taxes: Decimal
    dividends: Decimal
    position_quantities: tuple[tuple[str, int], ...]
    position_marks: tuple[tuple[str, Decimal, str, str, bool, int], ...]
    valuation_complete: bool
    reconciliation_residual: Decimal
    checksum: str


class AccountingInvariantError(RuntimeError):
    pass


class PortfolioLedger:
    def __init__(
        self,
        portfolio_id: str,
        initial_cash: Decimal,
        settlement_policy: SettlementPolicy,
        opened_at: datetime,
        mark_policy: MarkPolicy | None = None,
    ) -> None:
        if not portfolio_id or initial_cash <= ZERO:
            raise ValueError("portfolio id and positive initial cash are required")
        if opened_at.tzinfo is None or opened_at.utcoffset() is None:
            raise ValueError("ledger opening time must be timezone-aware")
        self.portfolio_id = portfolio_id
        self.initial_cash = initial_cash
        self.settlement_policy = settlement_policy
        self.mark_policy = mark_policy or MarkPolicy()
        self.settled_cash = initial_cash
        self.positions: dict[str, PositionState] = {}
        self.pending_settlements: list[PendingSettlement] = []
        self.dividend_receivables: dict[tuple[str, date], Decimal] = {}
        self.corporate_action_ids: set[str] = set()
        self.transactions: list[AccountingTransaction] = []
        self.realized_pnl = ZERO
        self.total_fees = ZERO
        self.total_taxes = ZERO
        self.total_dividends = ZERO
        self._transaction_sequence = 0
        self._record(
            opened_at,
            "opening_balance",
            portfolio_id,
            (Posting("cash", initial_cash), Posting("contributed_capital", -initial_cash)),
            {"currency": "TWD"},
        )

    @property
    def settlement_payables(self) -> Decimal:
        return sum(
            (
                item.amount
                for item in self.pending_settlements
                if not item.settled and item.side is Side.BUY
            ),
            ZERO,
        )

    @property
    def settlement_receivables_total(self) -> Decimal:
        return sum(
            (
                item.amount
                for item in self.pending_settlements
                if not item.settled and item.side is Side.SELL
            ),
            ZERO,
        )

    @property
    def dividend_receivables_total(self) -> Decimal:
        return sum(self.dividend_receivables.values(), ZERO)

    @property
    def available_cash(self) -> Decimal:
        return self.settled_cash - self.settlement_payables

    def quantity(self, instrument: Instrument) -> int:
        state = self.positions.get(instrument.key)
        return state.quantity if state is not None else 0

    def apply_fill(
        self,
        fill: Fill,
        trade_date: date,
        trading_sessions: tuple[date, ...],
    ) -> PendingSettlement:
        if fill.instrument.currency != "TWD":
            raise AccountingInvariantError("Slice 1 accounting accepts TWD instruments only")
        gross = fill.price * fill.quantity
        state = self.positions.get(fill.instrument.key)
        if fill.side is Side.BUY:
            total = gross + fill.fee
            if total > self.available_cash:
                raise AccountingInvariantError("buy fill exceeds available settled cash")
            if state is None:
                state = PositionState(
                    fill.instrument,
                    market_price=fill.price,
                    mark_timestamp=fill.event_time,
                    mark_source="execution_fill",
                )
                self.positions[fill.instrument.key] = state
            old_cost = state.cost_basis
            new_quantity = state.quantity + fill.quantity
            state.quantity = new_quantity
            state.book_cost = old_cost + total
            state.average_cost = state.book_cost / new_quantity
            postings: tuple[Posting, ...] = (
                Posting(f"inventory:{fill.instrument.key}", total),
                Posting("settlement_payable", -total),
            )
            settlement_amount = total
        else:
            if state is None or fill.quantity > state.quantity:
                raise AccountingInvariantError("sell fill would create a short position")
            old_quantity = state.quantity
            cost_basis = state.book_cost * fill.quantity / old_quantity
            net = gross - fill.fee - fill.tax
            state.quantity -= fill.quantity
            state.book_cost -= cost_basis
            self.realized_pnl += net - cost_basis
            if state.quantity == 0:
                state.average_cost = ZERO
                state.book_cost = ZERO
            else:
                state.average_cost = state.book_cost / state.quantity
            postings = (
                Posting("settlement_receivable", net),
                Posting("commission_expense", fill.fee),
                Posting("transaction_tax_expense", fill.tax),
                Posting("cost_of_sales", cost_basis),
                Posting(f"inventory:{fill.instrument.key}", -cost_basis),
                Posting("sale_proceeds", -gross),
            )
            settlement_amount = net
        state.market_price = fill.price
        state.mark_timestamp = fill.event_time
        state.mark_source = "execution_fill"
        state.stale_sessions = 0
        self.total_fees += fill.fee
        self.total_taxes += fill.tax
        due_date = self.settlement_policy.due_date(trade_date, trading_sessions)
        pending = PendingSettlement(
            settlement_id=f"settlement:{fill.fill_id}",
            due_date=due_date,
            side=fill.side,
            amount=settlement_amount,
        )
        self.pending_settlements.append(pending)
        self._record(
            fill.event_time,
            "trade_fill",
            fill.fill_id,
            postings,
            {
                "side": fill.side.value,
                "quantity": str(fill.quantity),
                "price": str(fill.price),
                "fee": str(fill.fee),
                "tax": str(fill.tax),
                "settlement_due": due_date.isoformat(),
            },
        )
        self.reconcile()
        return pending

    def process_settlements(self, current_date: date, occurred_at: datetime) -> tuple[str, ...]:
        processed: list[str] = []
        replacements: list[PendingSettlement] = []
        for item in self.pending_settlements:
            if item.settled or item.due_date > current_date:
                replacements.append(item)
                continue
            if item.side is Side.BUY:
                if item.amount > self.settled_cash:
                    raise AccountingInvariantError("settlement payable exceeds settled cash")
                self.settled_cash -= item.amount
                postings = (
                    Posting("settlement_payable", item.amount),
                    Posting("cash", -item.amount),
                )
            else:
                self.settled_cash += item.amount
                postings = (
                    Posting("cash", item.amount),
                    Posting("settlement_receivable", -item.amount),
                )
            replacements.append(
                PendingSettlement(
                    item.settlement_id, item.due_date, item.side, item.amount, settled=True
                )
            )
            processed.append(item.settlement_id)
            self._record(
                occurred_at,
                "cash_settlement",
                item.settlement_id,
                postings,
                {"due_date": item.due_date.isoformat(), "side": item.side.value},
            )
        self.pending_settlements = replacements
        self._pay_dividends(current_date, occurred_at, processed)
        self.reconcile()
        return tuple(processed)

    def accrue_dividend(self, action: CashDividend, occurred_at: datetime) -> Decimal:
        if action.action_id in self.corporate_action_ids:
            raise AccountingInvariantError("dividend action was already accrued")
        quantity = self.quantity(action.instrument)
        amount = action.amount_per_share * quantity
        key = (action.action_id, action.pay_date)
        if key in self.dividend_receivables:
            raise AccountingInvariantError("dividend action was already accrued")
        self.dividend_receivables[key] = amount
        self.corporate_action_ids.add(action.action_id)
        self.total_dividends += amount
        self._record(
            occurred_at,
            "cash_dividend_accrual",
            action.action_id,
            (Posting("dividend_receivable", amount), Posting("dividend_income", -amount)),
            {
                "instrument": action.instrument.key,
                "quantity": str(quantity),
                "amount_per_share": str(action.amount_per_share),
                "pay_date": action.pay_date.isoformat(),
                "record_date": action.record_date.isoformat(),
                "entitled_quantity": str(quantity),
                "current_quantity": str(quantity),
                "action_version": action.action_version,
                "policy_version": action.policy_version,
            },
        )
        self.reconcile()
        return amount

    def apply_split(self, action: StockSplit, occurred_at: datetime) -> tuple[int, int]:
        if action.action_id in self.corporate_action_ids:
            raise AccountingInvariantError("stock split action was already applied")
        state = self.positions.get(action.instrument.key)
        if state is None or state.quantity == 0:
            old_quantity = 0
            new_quantity = 0
        else:
            old_quantity = state.quantity
            exact_quantity = Decimal(old_quantity) * action.ratio
            if exact_quantity != exact_quantity.to_integral_value():
                raise AccountingInvariantError("stock split produces fractional shares")
            new_quantity = int(exact_quantity)
            if new_quantity <= 0:
                raise AccountingInvariantError("stock split produces invalid quantity")
            state.quantity = new_quantity
            state.average_cost = state.book_cost / new_quantity
            state.market_price /= action.ratio
        self._record(
            occurred_at,
            "stock_split",
            action.action_id,
            (),
            {
                "instrument": action.instrument.key,
                "ratio": str(action.ratio),
                "old_quantity": str(old_quantity),
                "new_quantity": str(new_quantity),
                "action_version": action.action_version,
                "policy_version": action.policy_version,
            },
        )
        self.corporate_action_ids.add(action.action_id)
        self.reconcile()
        return old_quantity, new_quantity

    def mark(
        self, instrument: Instrument, price: Decimal, marked_at: datetime, source: str
    ) -> None:
        if price <= ZERO:
            raise AccountingInvariantError("mark price must be positive")
        if marked_at.tzinfo is None or marked_at.utcoffset() is None or not source:
            raise AccountingInvariantError("mark timestamp and source are required")
        state = self.positions.get(instrument.key)
        if state is not None:
            state.market_price = price
            state.mark_timestamp = marked_at
            state.mark_source = source
            state.stale_sessions = 0

    def age_unmarked_positions(self, marked_instrument_keys: set[str]) -> None:
        for key, state in self.positions.items():
            if state.quantity and key not in marked_instrument_keys:
                state.stale_sessions += 1

    def snapshot(self, as_of: datetime) -> AccountingSnapshot:
        self.reconcile()
        market_value = sum((state.market_value for state in self.positions.values()), ZERO)
        unrealized = sum((state.unrealized_pnl for state in self.positions.values()), ZERO)
        nav = (
            self.settled_cash
            + self.settlement_receivables_total
            - self.settlement_payables
            + self.dividend_receivables_total
            + market_value
        )
        quantities = tuple(
            sorted(
                (key, state.quantity)
                for key, state in self.positions.items()
                if state.quantity != 0
            )
        )
        position_marks = tuple(
            sorted(
                (
                    key,
                    state.market_price,
                    state.mark_timestamp.isoformat() if state.mark_timestamp else "",
                    state.mark_source or "",
                    state.stale_sessions > 0,
                    state.stale_sessions,
                )
                for key, state in self.positions.items()
                if state.quantity != 0
            )
        )
        valuation_complete = all(
            state.mark_timestamp is not None and state.mark_source is not None
            for state in self.positions.values()
            if state.quantity != 0
        )
        if any(
            state.stale_sessions > self.mark_policy.maximum_stale_sessions
            for state in self.positions.values()
            if state.quantity != 0
        ):
            raise AccountingInvariantError("maximum stale mark sessions exceeded")
        reconciliation_residual = ZERO
        payload = {
            "as_of": as_of.isoformat(),
            "settled_cash": str(self.settled_cash),
            "available_cash": str(self.available_cash),
            "settlement_receivables": str(self.settlement_receivables_total),
            "settlement_payables": str(self.settlement_payables),
            "dividend_receivables": str(self.dividend_receivables_total),
            "positions_market_value": str(market_value),
            "net_asset_value": str(nav),
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(unrealized),
            "fees": str(self.total_fees),
            "taxes": str(self.total_taxes),
            "dividends": str(self.total_dividends),
            "position_quantities": quantities,
            "position_marks": position_marks,
            "valuation_complete": valuation_complete,
            "reconciliation_residual": str(reconciliation_residual),
        }
        checksum = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        return AccountingSnapshot(
            as_of,
            self.settled_cash,
            self.available_cash,
            self.settlement_receivables_total,
            self.settlement_payables,
            self.dividend_receivables_total,
            market_value,
            nav,
            self.realized_pnl,
            unrealized,
            self.total_fees,
            self.total_taxes,
            self.total_dividends,
            quantities,
            position_marks,
            valuation_complete,
            reconciliation_residual,
            checksum,
        )

    def reconcile(self) -> None:
        if any(
            previous.occurred_at > current.occurred_at
            for previous, current in zip(self.transactions, self.transactions[1:], strict=False)
        ):
            raise AccountingInvariantError("accounting journal is not chronological")
        if any(
            sum((posting.amount for posting in tx.postings), ZERO) != ZERO
            for tx in self.transactions
        ):
            raise AccountingInvariantError("unbalanced accounting transaction detected")
        if self.available_cash < ZERO:
            raise AccountingInvariantError("available cash is negative")
        balances: dict[str, Decimal] = {}
        for transaction in self.transactions:
            for posting in transaction.postings:
                balances[posting.account] = balances.get(posting.account, ZERO) + posting.amount
        expected_balances = {
            "cash": self.settled_cash,
            "settlement_payable": -self.settlement_payables,
            "settlement_receivable": self.settlement_receivables_total,
            "dividend_receivable": self.dividend_receivables_total,
            "contributed_capital": -self.initial_cash,
            "transaction_tax_expense": self.total_taxes,
            "dividend_income": -self.total_dividends,
        }
        for account, expected in expected_balances.items():
            if balances.get(account, ZERO) != expected:
                raise AccountingInvariantError(f"{account} does not reconcile to ledger state")
        fees_from_fills = sum(
            (
                Decimal(transaction.metadata["fee"])
                for transaction in self.transactions
                if transaction.kind == "trade_fill"
            ),
            ZERO,
        )
        if fees_from_fills != self.total_fees:
            raise AccountingInvariantError("fees do not reconcile to fill journal")
        derived_realized = -(
            balances.get("sale_proceeds", ZERO)
            + balances.get("cost_of_sales", ZERO)
            + balances.get("commission_expense", ZERO)
            + balances.get("transaction_tax_expense", ZERO)
        )
        if derived_realized != self.realized_pnl:
            raise AccountingInvariantError("realized PnL does not reconcile to journal")
        for state in self.positions.values():
            if state.quantity < 0 or not isinstance(state.quantity, int):
                raise AccountingInvariantError("position quantity violates long-only integers")
            if state.average_cost < ZERO or state.market_price < ZERO:
                raise AccountingInvariantError("position valuation is negative")
            inventory = balances.get(f"inventory:{state.instrument.key}", ZERO)
            if inventory != state.cost_basis:
                message = "position cost basis does not reconcile to inventory"
                raise AccountingInvariantError(message)

    def journal_checksum(self) -> str:
        payload = [
            {
                "id": transaction.transaction_id,
                "time": transaction.occurred_at.isoformat(),
                "kind": transaction.kind,
                "reference": transaction.reference_id,
                "postings": [
                    (posting.account, str(posting.amount)) for posting in transaction.postings
                ],
                "metadata": transaction.metadata,
            }
            for transaction in self.transactions
        ]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _pay_dividends(
        self, current_date: date, occurred_at: datetime, processed: list[str]
    ) -> None:
        for key, amount in list(self.dividend_receivables.items()):
            action_id, pay_date = key
            if pay_date > current_date:
                continue
            self.settled_cash += amount
            self._record(
                occurred_at,
                "cash_dividend_payment",
                action_id,
                (Posting("cash", amount), Posting("dividend_receivable", -amount)),
                {"pay_date": pay_date.isoformat()},
            )
            processed.append(f"dividend:{action_id}")
            del self.dividend_receivables[key]

    def _record(
        self,
        occurred_at: datetime,
        kind: str,
        reference_id: str,
        postings: tuple[Posting, ...],
        metadata: dict[str, str],
    ) -> None:
        self._transaction_sequence += 1
        raw = f"{self.portfolio_id}:{self._transaction_sequence}:{kind}:{reference_id}"
        transaction_id = hashlib.sha256(raw.encode()).hexdigest()
        self.transactions.append(
            AccountingTransaction(
                transaction_id, occurred_at, kind, reference_id, postings, metadata
            )
        )
