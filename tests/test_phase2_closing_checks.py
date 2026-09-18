from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from island_quant.backtest.engine import EventDrivenBacktestEngine
from island_quant.backtest.events import BacktestEvent, BacktestEventKind, DeterministicEventJournal
from island_quant.backtest.fixtures import synthetic_backtest_fixture
from island_quant.backtest.policies import (
    FeeTaxPolicy,
    FillPolicy,
    MarkPolicy,
    RiskPolicy,
    SettlementPolicy,
    TickSizePolicy,
)
from island_quant.backtest.replay import ReplayMismatchError, replay_events
from island_quant.domain.models import Fill, Instrument, Market, OrderIntent, OrderType, Side
from island_quant.execution.simulation import ConservativeFillSimulator, ExecutionQuote
from island_quant.portfolio.accounting import (
    AccountingInvariantError,
    CashDividend,
    PortfolioLedger,
)
from island_quant.reports.backtest import build_backtest_report
from island_quant.risk.pretrade import PreTradeRiskEngine
from island_quant.risk.reservations import (
    PendingOrderBook,
    ReservationError,
    ReservationStatus,
)
from island_quant.strategies.fixtures import EqualWeightFixtureStrategy

TAIPEI = ZoneInfo("Asia/Taipei")
STOCK = Instrument("2330", Market.TWSE)
CALENDAR = tuple(
    date(2024, 1, 8 + offset) for offset in range(8) if date(2024, 1, 8 + offset).weekday() < 5
)


def at(day: date, hour: int = 9) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=TAIPEI)


def make_fill(
    identity: str,
    side: Side,
    quantity: int,
    price: str,
    fee: str,
    tax: str,
    day: date,
    client: str | None = None,
) -> Fill:
    return Fill(
        identity,
        client or f"client-{identity}",
        STOCK,
        side,
        quantity,
        Decimal(price),
        Decimal(fee),
        Decimal(tax),
        at(day),
        at(day),
    )


def make_intent(identity: int, side: Side, quantity: int) -> OrderIntent:
    return OrderIntent(
        "portfolio",
        "strategy",
        STOCK,
        side,
        quantity,
        OrderType.MARKET,
        at(CALENDAR[0], 8),
        f"idempotency-{identity}",
        intent_id=UUID(int=identity),
    )


def rebuild_events(events: list[BacktestEvent]) -> tuple[BacktestEvent, ...]:
    journal = DeterministicEventJournal(events[0].run_id, events[0].schema_version)
    for event in events:
        journal.append(
            event.kind,
            event.occurred_at,
            event.payload,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
        )
    return journal.events


def test_decimal_precision_is_exact_across_many_small_fills_and_snapshot() -> None:
    ledger = PortfolioLedger("precision", Decimal("1000"), SettlementPolicy(), at(CALENDAR[0], 8))
    for index in range(1000):
        ledger.apply_fill(
            make_fill(str(index), Side.BUY, 1, "0.1", "0", "0", CALENDAR[0]),
            CALENDAR[0],
            CALENDAR,
        )
    snapshot = ledger.snapshot(at(CALENDAR[0], 17))
    assert ledger.positions[STOCK.key].book_cost == Decimal("100.0")
    assert snapshot.reconciliation_residual == Decimal("0")
    assert snapshot.net_asset_value == Decimal("1000.0")
    assert all(
        sum((posting.amount for posting in transaction.postings), Decimal("0")) == Decimal("0")
        for transaction in ledger.transactions
    )


def test_decimal_serialization_and_replay_preserve_exact_values() -> None:
    fixture = synthetic_backtest_fixture("decimal-replay")
    result = EventDrivenBacktestEngine().run(
        fixture.config, fixture.sessions, EqualWeightFixtureStrategy()
    )
    replay = replay_events(result.events)
    assert replay.final_snapshot_checksum == result.final_snapshot.checksum
    assert isinstance(result.final_snapshot.net_asset_value, Decimal)
    fill_events = [
        event for event in result.events if event.kind is BacktestEventKind.FILL_RECEIVED
    ]
    assert all(
        isinstance(event.payload["fill"]["price"], str)  # type: ignore[index]
        for event in fill_events
        if event.payload["status"] != "unfilled"
    )


def test_pending_buy_reservations_reduce_buying_power_and_are_idempotent() -> None:
    fee_policy = FeeTaxPolicy()
    ledger = PortfolioLedger(
        "reservations", Decimal("1000"), SettlementPolicy(), at(CALENDAR[0], 7)
    )
    book = PendingOrderBook(fee_policy)
    risk = PreTradeRiskEngine(RiskPolicy(maximum_position_weight=Decimal("1")), fee_policy)
    first = make_intent(1, Side.BUY, 6)
    first_decision = risk.evaluate(
        first,
        estimated_price=Decimal("100"),
        ledger=ledger,
        net_asset_value=Decimal("1000"),
        evaluated_at=at(CALENDAR[0]),
        pending_orders=book,
    )
    assert first_decision.outcome.value == "approve"
    reservation = book.reserve(first, Decimal("100"))
    assert reservation.reserved_cash == Decimal("620")
    assert book.reserve(first, Decimal("100")) == reservation
    assert book.reserved_cash == Decimal("620")
    second = make_intent(2, Side.BUY, 5)
    second_decision = risk.evaluate(
        second,
        estimated_price=Decimal("100"),
        ledger=ledger,
        net_asset_value=Decimal("1000"),
        evaluated_at=at(CALENDAR[0]),
        pending_orders=book,
    )
    assert second_decision.outcome.value == "reject"
    assert "insufficient_available_cash" in second_decision.reasons
    book.release(str(first.intent_id), ReservationStatus.CANCELLED)
    assert book.reserved_cash == Decimal("0")


@pytest.mark.parametrize(
    "terminal", [ReservationStatus.CANCELLED, ReservationStatus.EXPIRED, ReservationStatus.REJECTED]
)
def test_terminal_order_states_release_cash(terminal: ReservationStatus) -> None:
    book = PendingOrderBook(FeeTaxPolicy())
    order = make_intent(3, Side.BUY, 5)
    book.reserve(order, Decimal("100"))
    book.release(str(order.intent_id), terminal)
    assert book.reserved_cash == Decimal("0")


def test_partial_fill_adjusts_actual_cost_and_releases_only_unused_reservation() -> None:
    book = PendingOrderBook(FeeTaxPolicy())
    order = make_intent(4, Side.BUY, 10)
    book.reserve(order, Decimal("100"))
    partial = make_fill("partial", Side.BUY, 4, "90", "20", "0", CALENDAR[0], order.idempotency_key)
    updated = book.apply_fill(str(order.intent_id), partial)
    assert updated.remaining_quantity == 6
    assert updated.reserved_cash == Decimal("600")
    assert book.reserved_cash == Decimal("600")
    released = book.release(str(order.intent_id), ReservationStatus.CANCELLED)
    assert released.remaining_quantity == 6 and book.reserved_cash == 0


def test_pending_orders_are_included_in_position_gross_and_sellable_quantity() -> None:
    ledger = PortfolioLedger("projected", Decimal("1000"), SettlementPolicy(), at(CALENDAR[0], 7))
    fee_policy = FeeTaxPolicy(minimum_commission=Decimal("0"))
    book = PendingOrderBook(fee_policy)
    risk = PreTradeRiskEngine(RiskPolicy(maximum_position_weight=Decimal("0.6")), fee_policy)
    first = make_intent(5, Side.BUY, 5)
    assert (
        risk.evaluate(
            first,
            estimated_price=Decimal("100"),
            ledger=ledger,
            net_asset_value=Decimal("1000"),
            evaluated_at=at(CALENDAR[0]),
            pending_orders=book,
        ).outcome.value
        == "approve"
    )
    book.reserve(first, Decimal("100"))
    second = make_intent(6, Side.BUY, 2)
    decision = risk.evaluate(
        second,
        estimated_price=Decimal("100"),
        ledger=ledger,
        net_asset_value=Decimal("1000"),
        evaluated_at=at(CALENDAR[0]),
        pending_orders=book,
    )
    assert "maximum_position_weight_exceeded" in decision.reasons

    holding = PortfolioLedger("sellable", Decimal("2000"), SettlementPolicy(), at(CALENDAR[0], 7))
    holding.apply_fill(
        make_fill("owned", Side.BUY, 10, "10", "0", "0", CALENDAR[0]), CALENDAR[0], CALENDAR
    )
    sell_book = PendingOrderBook(fee_policy)
    first_sell = make_intent(7, Side.SELL, 8)
    sell_book.reserve(first_sell, Decimal("10"))
    second_sell = make_intent(8, Side.SELL, 3)
    sell_decision = risk.evaluate(
        second_sell,
        estimated_price=Decimal("10"),
        ledger=holding,
        net_asset_value=Decimal("2000"),
        evaluated_at=at(CALENDAR[0]),
        pending_orders=sell_book,
    )
    assert "short_position_forbidden" in sell_decision.reasons


def test_order_lifecycle_minimum_commission_is_not_repeated_across_fills() -> None:
    policy = FeeTaxPolicy()
    first_fee, _ = policy.incremental_costs(Side.BUY, Decimal("0"), Decimal("100"), Decimal("0"))
    second_fee, _ = policy.incremental_costs(Side.BUY, Decimal("100"), Decimal("100"), first_fee)
    cross_day_fee, _ = policy.incremental_costs(
        Side.BUY, Decimal("200"), Decimal("100"), first_fee + second_fee
    )
    assert (first_fee, second_fee, cross_day_fee) == (
        Decimal("20"),
        Decimal("0"),
        Decimal("0"),
    )
    independent_fee, _ = policy.incremental_costs(
        Side.BUY, Decimal("0"), Decimal("100"), Decimal("0")
    )
    assert independent_fee == Decimal("20")
    assert policy.minimum_commission_scope == "per_order_lifecycle"
    assert policy.purpose == "engineering_fixture" and policy.verified_source is None


def test_t_plus_two_uses_only_pinned_open_sessions_and_fails_closed() -> None:
    policy = SettlementPolicy()
    monday_calendar = (date(2024, 1, 8), date(2024, 1, 9), date(2024, 1, 10))
    assert policy.due_date(date(2024, 1, 8), monday_calendar) == date(2024, 1, 10)
    thursday_calendar = (date(2024, 1, 11), date(2024, 1, 12), date(2024, 1, 15))
    assert policy.due_date(date(2024, 1, 11), thursday_calendar) == date(2024, 1, 15)
    holiday_calendar = (date(2024, 1, 8), date(2024, 1, 9), date(2024, 1, 11))
    assert policy.due_date(date(2024, 1, 8), holiday_calendar) == date(2024, 1, 11)
    closure_calendar = (date(2024, 1, 8), date(2024, 1, 10), date(2024, 1, 11))
    assert policy.due_date(date(2024, 1, 8), closure_calendar) == date(2024, 1, 11)
    with pytest.raises(ValueError, match="lacks required settlement"):
        policy.due_date(date(2024, 1, 8), monday_calendar[:2])


def test_manifest_records_real_settlement_extension_sessions() -> None:
    fixture = synthetic_backtest_fixture("manifest")
    result = EventDrivenBacktestEngine().run(
        fixture.config, fixture.sessions, EqualWeightFixtureStrategy()
    )
    final_trade = fixture.sessions[-1].trade_date
    final_index = fixture.config.trading_calendar.index(final_trade)
    assert (
        result.manifest.settlement_extension_sessions
        == fixture.config.trading_calendar[final_index + 1 : final_index + 3]
    )
    truncated = replace(
        fixture.config, trading_calendar=fixture.config.trading_calendar[: final_index + 2]
    )
    with pytest.raises(ValueError, match="final settlement"):
        EventDrivenBacktestEngine().run(truncated, fixture.sessions, EqualWeightFixtureStrategy())


def test_unsettled_sale_proceeds_do_not_increase_buying_power_and_settlement_is_idempotent() -> (
    None
):
    ledger = PortfolioLedger(
        "buying-power", Decimal("2000"), SettlementPolicy(), at(CALENDAR[0], 7)
    )
    ledger.apply_fill(
        make_fill("buy", Side.BUY, 10, "100", "20", "0", CALENDAR[0]), CALENDAR[0], CALENDAR
    )
    assert ledger.available_cash == Decimal("980")
    ledger.apply_fill(
        make_fill("sell", Side.SELL, 10, "110", "20", "3", CALENDAR[0]), CALENDAR[0], CALENDAR
    )
    assert ledger.settlement_receivables_total == Decimal("1077")
    assert ledger.available_cash == Decimal("980")
    risk = PreTradeRiskEngine(
        RiskPolicy(maximum_position_weight=Decimal("1")), FeeTaxPolicy()
    )
    same_day_buy = risk.evaluate(
        make_intent(20, Side.BUY, 10),
        estimated_price=Decimal("100"),
        ledger=ledger,
        net_asset_value=Decimal("2000"),
        evaluated_at=at(CALENDAR[0], 10),
    )
    assert "insufficient_available_cash" in same_day_buy.reasons
    assert len({item.due_date for item in ledger.pending_settlements}) == 1
    processed = ledger.process_settlements(CALENDAR[2], at(CALENDAR[2], 8))
    cash = ledger.settled_cash
    assert len(processed) == 2
    assert ledger.process_settlements(CALENDAR[2], at(CALENDAR[2], 8)) == ()
    assert ledger.settled_cash == cash
    assert ledger.settlement_policy.obligation_netting == "gross_per_obligation"


def test_multiple_settlement_dates_remain_separate_obligations() -> None:
    ledger = PortfolioLedger(
        "multiple-dates", Decimal("5000"), SettlementPolicy(), at(CALENDAR[0], 7)
    )
    ledger.apply_fill(
        make_fill("day-one", Side.BUY, 1, "100", "0", "0", CALENDAR[0]),
        CALENDAR[0],
        CALENDAR,
    )
    ledger.apply_fill(
        make_fill("day-two", Side.BUY, 1, "100", "0", "0", CALENDAR[1]),
        CALENDAR[1],
        CALENDAR,
    )
    assert [item.due_date for item in ledger.pending_settlements] == [
        CALENDAR[2],
        CALENDAR[3],
    ]


def test_average_cost_multiple_buys_partial_close_full_close_and_reentry() -> None:
    ledger = PortfolioLedger("average", Decimal("10000"), SettlementPolicy(), at(CALENDAR[0], 7))
    ledger.apply_fill(
        make_fill("a", Side.BUY, 100, "10", "20", "0", CALENDAR[0]), CALENDAR[0], CALENDAR
    )
    ledger.apply_fill(
        make_fill("b", Side.BUY, 50, "20", "20", "0", CALENDAR[0]), CALENDAR[0], CALENDAR
    )
    state = ledger.positions[STOCK.key]
    assert state.book_cost == Decimal("2040") and state.average_cost == Decimal("13.6")
    ledger.apply_fill(
        make_fill("c", Side.SELL, 60, "30", "20", "5", CALENDAR[0]), CALENDAR[0], CALENDAR
    )
    assert state.quantity == 90 and state.book_cost == Decimal("1224.0")
    ledger.apply_fill(
        make_fill("d", Side.SELL, 90, "30", "20", "8", CALENDAR[0]), CALENDAR[0], CALENDAR
    )
    assert state.quantity == 0 and state.book_cost == Decimal("0")
    ledger.apply_fill(
        make_fill("e", Side.BUY, 100, "5", "20", "0", CALENDAR[0]), CALENDAR[0], CALENDAR
    )
    assert state.average_cost == Decimal("5.2")


def test_fill_ordering_does_not_change_average_cost_state() -> None:
    def state_for(order: tuple[str, str]) -> tuple[int, Decimal, Decimal]:
        ledger = PortfolioLedger(
            "ordering", Decimal("10000"), SettlementPolicy(), at(CALENDAR[0], 7)
        )
        for identity, price in order:
            ledger.apply_fill(
                make_fill(identity, Side.BUY, 10, price, "1", "0", CALENDAR[0]),
                CALENDAR[0],
                CALENDAR,
            )
        state = ledger.positions[STOCK.key]
        return state.quantity, state.book_cost, state.average_cost

    assert state_for((("a", "10"), ("b", "20"))) == state_for((("b", "20"), ("a", "10")))


def test_last_valid_mark_is_stale_not_zero_and_excess_age_fails_closed() -> None:
    ledger = PortfolioLedger(
        "marks",
        Decimal("1000"),
        SettlementPolicy(),
        at(CALENDAR[0], 7),
        MarkPolicy(maximum_stale_sessions=2),
    )
    ledger.apply_fill(
        make_fill("mark-buy", Side.BUY, 2, "100", "0", "0", CALENDAR[0]), CALENDAR[0], CALENDAR
    )
    ledger.mark(STOCK, Decimal("110"), at(CALENDAR[0], 13), "official_close_fixture")
    ledger.age_unmarked_positions(set())
    snapshot = ledger.snapshot(at(CALENDAR[1], 17))
    mark = snapshot.position_marks[0]
    assert mark[1] == Decimal("110") and mark[4] is True and mark[5] == 1
    assert snapshot.positions_market_value == Decimal("220")
    ledger.age_unmarked_positions(set())
    ledger.snapshot(at(CALENDAR[2], 17))
    ledger.age_unmarked_positions(set())
    with pytest.raises(AccountingInvariantError, match="stale"):
        ledger.snapshot(at(CALENDAR[3], 17))

    incomplete_ledger = PortfolioLedger(
        "missing-mark", Decimal("1000"), SettlementPolicy(), at(CALENDAR[0], 7)
    )
    incomplete_ledger.apply_fill(
        make_fill("missing", Side.BUY, 1, "100", "0", "0", CALENDAR[0]),
        CALENDAR[0],
        CALENDAR,
    )
    incomplete_ledger.positions[STOCK.key].mark_timestamp = None
    incomplete = incomplete_ledger.snapshot(at(CALENDAR[0], 17))
    assert incomplete.valuation_complete is False
    assert incomplete.positions_market_value == Decimal("100")


def test_incomplete_valuation_cannot_be_promoted() -> None:
    fixture = synthetic_backtest_fixture("incomplete-valuation")
    result = EventDrivenBacktestEngine().run(
        fixture.config, fixture.sessions, EqualWeightFixtureStrategy()
    )
    incomplete = replace(result.final_snapshot, valuation_complete=False)
    report = build_backtest_report(replace(result, final_snapshot=incomplete))
    assert report.valuation_complete is False and report.promotion_eligible is False


@pytest.mark.parametrize(
    ("side", "reference", "slippage", "expected"),
    [
        (Side.BUY, "49.99", "10", "50.1"),
        (Side.SELL, "50.01", "10", "49.95"),
        (Side.BUY, "50", "0", "50"),
        (Side.SELL, "50", "0", "50"),
        (Side.BUY, "9.99", "20", "10.05"),
    ],
)
def test_tick_band_rounding_is_adverse_and_legal(
    side: Side, reference: str, slippage: str, expected: str
) -> None:
    tick_policy = TickSizePolicy()
    simulator = ConservativeFillSimulator(
        FillPolicy(slippage_bps=Decimal(slippage)), FeeTaxPolicy(), tick_policy
    )
    order = make_intent(10 if side is Side.BUY else 11, side, 1)
    quote = ExecutionQuote(STOCK, at(CALENDAR[0], 9), Decimal(reference), Decimal(reference), 100)
    price = simulator.estimated_price(order, quote)
    assert price == Decimal(expected) and price > 0
    assert price / tick_policy.tick(price) == (price / tick_policy.tick(price)).to_integral()
    assert price >= quote.open_price if side is Side.BUY else price <= quote.open_price


def test_dividend_entitlement_is_fixed_on_ex_date_not_payment_quantity() -> None:
    ledger = PortfolioLedger("entitlement", Decimal("5000"), SettlementPolicy(), at(CALENDAR[0], 7))
    ledger.apply_fill(
        make_fill("owned", Side.BUY, 10, "100", "0", "0", CALENDAR[0]), CALENDAR[0], CALENDAR
    )
    action = CashDividend(
        "dividend",
        STOCK,
        CALENDAR[1],
        CALENDAR[1],
        CALENDAR[3],
        Decimal("2"),
        at(CALENDAR[0], 18),
        "announcement-v1",
    )
    assert ledger.accrue_dividend(action, at(CALENDAR[1], 8)) == Decimal("20")
    ledger.apply_fill(
        make_fill("sold", Side.SELL, 10, "100", "0", "0", CALENDAR[1]), CALENDAR[1], CALENDAR
    )
    ledger.apply_fill(
        make_fill("added", Side.BUY, 20, "100", "0", "0", CALENDAR[2]), CALENDAR[2], CALENDAR
    )
    cash_before = ledger.settled_cash
    processed = ledger.process_settlements(CALENDAR[3], at(CALENDAR[3], 8))
    assert "dividend:dividend" in processed
    assert ledger.settled_cash >= cash_before + Decimal("20")
    with pytest.raises(AccountingInvariantError, match="already accrued"):
        ledger.accrue_dividend(action, at(CALENDAR[1], 8))


def test_sold_before_ex_date_has_zero_entitlement_and_missing_semantics_fail() -> None:
    ledger = PortfolioLedger(
        "no-entitlement", Decimal("5000"), SettlementPolicy(), at(CALENDAR[0], 7)
    )
    ledger.apply_fill(
        make_fill("owned", Side.BUY, 10, "100", "0", "0", CALENDAR[0]), CALENDAR[0], CALENDAR
    )
    ledger.apply_fill(
        make_fill("sold", Side.SELL, 10, "100", "0", "0", CALENDAR[0]), CALENDAR[0], CALENDAR
    )
    action = CashDividend(
        "zero-dividend",
        STOCK,
        CALENDAR[1],
        CALENDAR[1],
        CALENDAR[3],
        Decimal("2"),
        at(CALENDAR[0], 18),
        "announcement-v1",
    )
    assert ledger.accrue_dividend(action, at(CALENDAR[1], 8)) == Decimal("0")
    with pytest.raises(ValueError, match="contract"):
        CashDividend(
            "bad", STOCK, CALENDAR[1], CALENDAR[1], CALENDAR[3], Decimal("2"), at(CALENDAR[0]), ""
        )


def test_replay_enforces_business_transitions_beyond_checksums() -> None:
    fixture = synthetic_backtest_fixture("business-replay")
    result = EventDrivenBacktestEngine().run(
        fixture.config, fixture.sessions, EqualWeightFixtureStrategy()
    )
    events = list(result.events)
    risk_index = next(
        index
        for index, event in enumerate(events)
        if event.kind is BacktestEventKind.RISK_DECIDED and event.payload["outcome"] == "approve"
    )
    events[risk_index] = replace(
        events[risk_index], payload={**events[risk_index].payload, "outcome": "reject"}
    )
    with pytest.raises(ReplayMismatchError, match="reservation"):
        replay_events(rebuild_events(events))


def test_replay_detects_duplicate_fill_business_identity_with_new_event_ids() -> None:
    fixture = synthetic_backtest_fixture("duplicate-fill-replay")
    result = EventDrivenBacktestEngine().run(
        fixture.config, fixture.sessions, EqualWeightFixtureStrategy()
    )
    events = list(result.events)
    fill_index = next(
        index
        for index, event in enumerate(events)
        if event.kind is BacktestEventKind.FILL_RECEIVED
        and event.payload["status"] in {"filled", "partial"}
    )
    duplicate = replace(events[fill_index], correlation_id="duplicate-business-fill")
    events.insert(fill_index + 1, duplicate)
    with pytest.raises(ReplayMismatchError, match="duplicate fill"):
        replay_events(rebuild_events(events))


def test_fill_cannot_exceed_reservation_and_run_completed_is_terminal() -> None:
    book = PendingOrderBook(FeeTaxPolicy())
    order = make_intent(12, Side.BUY, 1)
    book.reserve(order, Decimal("100"))
    too_large = make_fill(
        "too-large", Side.BUY, 2, "100", "20", "0", CALENDAR[0], order.idempotency_key
    )
    with pytest.raises(ReservationError, match="exceeds"):
        book.apply_fill(str(order.intent_id), too_large)

    fixture = synthetic_backtest_fixture("terminal-run")
    result = EventDrivenBacktestEngine().run(
        fixture.config, fixture.sessions, EqualWeightFixtureStrategy()
    )
    events = list(result.events)
    last = events[-1]
    events.append(
        replace(
            last,
            kind=BacktestEventKind.MARKET_OPENED,
            occurred_at=last.occurred_at,
            payload={"unexpected": True},
        )
    )
    with pytest.raises(ReplayMismatchError, match="terminal"):
        replay_events(rebuild_events(events))
