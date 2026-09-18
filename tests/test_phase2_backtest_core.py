from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from island_quant.backtest.contracts import BacktestSession, PredictionObservation
from island_quant.backtest.engine import EventDrivenBacktestEngine
from island_quant.backtest.events import BacktestEventKind, DeterministicEventJournal
from island_quant.backtest.fixtures import synthetic_backtest_fixture
from island_quant.backtest.policies import FeeTaxPolicy, FillPolicy, RiskPolicy, SettlementPolicy
from island_quant.backtest.replay import ReplayMismatchError, replay_events
from island_quant.domain.models import (
    Fill,
    Instrument,
    Market,
    OrderIntent,
    OrderType,
    RiskOutcome,
    Side,
)
from island_quant.execution.simulation import ConservativeFillSimulator, ExecutionQuote
from island_quant.portfolio.accounting import (
    AccountingInvariantError,
    CashDividend,
    PortfolioLedger,
    Posting,
    StockSplit,
)
from island_quant.reports.backtest import SYNTHETIC_WARNING, build_backtest_report
from island_quant.risk.pretrade import PreTradeRiskEngine
from island_quant.strategies.fixtures import (
    BuyAndHoldFixtureStrategy,
    EqualWeightFixtureStrategy,
    PredictionTopKFixtureStrategy,
)

TAIPEI = ZoneInfo("Asia/Taipei")
CALENDAR = (
    date(2024, 1, 5),
    date(2024, 1, 8),
    date(2024, 1, 9),
    date(2024, 1, 10),
    date(2024, 1, 11),
    date(2024, 1, 12),
)
INSTRUMENT = Instrument("2330", Market.TWSE)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TAIPEI)


def fill(
    fill_id: str,
    side: Side,
    quantity: int,
    price: str,
    fee: str,
    tax: str,
    day: date,
) -> Fill:
    return Fill(
        fill_id,
        f"client-{fill_id}",
        INSTRUMENT,
        side,
        quantity,
        Decimal(price),
        Decimal(fee),
        Decimal(tax),
        at(day, 9),
        at(day, 9),
    )


def intent(side: Side, quantity: int, day: date = CALENDAR[0]) -> OrderIntent:
    return OrderIntent(
        "portfolio",
        "strategy",
        INSTRUMENT,
        side,
        quantity,
        OrderType.MARKET,
        at(day, 8, 45),
        f"intent-{side.value}-{quantity}",
        intent_id=UUID("00000000-0000-0000-0000-000000000001"),
    )


def test_versioned_event_journal_is_deterministic_and_chronological() -> None:
    first = DeterministicEventJournal("run", 1)
    second = DeterministicEventJournal("run", 1)
    for journal in (first, second):
        journal.append(BacktestEventKind.RUN_STARTED, at(CALENDAR[0], 8), {"b": 2, "a": 1})
        journal.append(BacktestEventKind.RUN_COMPLETED, at(CALENDAR[0], 17), {"ok": True})
    assert first.events == second.events
    assert first.checksum() == second.checksum()
    assert first.events[0].schema_version == 1
    with pytest.raises(ValueError, match="chronological"):
        first.append(BacktestEventKind.MARKET_OPENED, at(CALENDAR[0], 9), {})
    with pytest.raises(ValueError, match="timezone-aware"):
        DeterministicEventJournal("run").append(
            BacktestEventKind.RUN_STARTED, datetime(2024, 1, 1), {}
        )


def test_fee_tax_and_t_plus_two_policies_are_explicit() -> None:
    policy = FeeTaxPolicy()
    assert policy.costs(Side.BUY, Decimal("10000")) == (Decimal("20"), Decimal("0"))
    assert policy.costs(Side.SELL, Decimal("4400")) == (Decimal("20"), Decimal("13"))
    settlement = SettlementPolicy()
    assert settlement.due_date(CALENDAR[0], CALENDAR) == date(2024, 1, 9)
    assert policy.version and settlement.version


def test_hand_calculated_average_cost_pnl_cash_and_settlement_reconcile() -> None:
    ledger = PortfolioLedger("manual", Decimal("100000"), SettlementPolicy(), at(CALENDAR[0], 8))
    buy = fill("buy", Side.BUY, 100, "100", "20", "0", CALENDAR[0])
    pending_buy = ledger.apply_fill(buy, CALENDAR[0], CALENDAR)
    after_buy = ledger.snapshot(at(CALENDAR[0], 16))
    assert pending_buy.due_date == date(2024, 1, 9)
    assert ledger.positions[INSTRUMENT.key].average_cost == Decimal("100.2")
    assert after_buy.available_cash == Decimal("89980")
    assert after_buy.net_asset_value == Decimal("99980")

    ledger.process_settlements(date(2024, 1, 9), at(date(2024, 1, 9), 8, 30))
    assert ledger.settled_cash == Decimal("89980")
    sell = fill("sell", Side.SELL, 40, "110", "20", "13", date(2024, 1, 10))
    pending_sell = ledger.apply_fill(sell, date(2024, 1, 10), CALENDAR)
    assert pending_sell.due_date == date(2024, 1, 12)
    assert ledger.realized_pnl == Decimal("359.0")
    ledger.process_settlements(date(2024, 1, 12), at(date(2024, 1, 12), 8, 30))
    ledger.mark(INSTRUMENT, Decimal("110"), at(date(2024, 1, 12), 13, 30), "test_close")
    final = ledger.snapshot(at(date(2024, 1, 12), 17))
    assert final.settled_cash == Decimal("94347")
    assert final.position_quantities == ((INSTRUMENT.key, 60),)
    assert final.unrealized_pnl == Decimal("588.0")
    assert final.net_asset_value == Decimal("100947")
    assert final.realized_pnl + final.unrealized_pnl == Decimal("947.0")
    assert all(sum(posting.amount for posting in tx.postings) == 0 for tx in ledger.transactions)
    ledger.reconcile()


def test_ledger_rejects_insufficient_cash_short_and_mutated_journal() -> None:
    ledger = PortfolioLedger("safe", Decimal("1000"), SettlementPolicy(), at(CALENDAR[0], 8))
    with pytest.raises(AccountingInvariantError, match="available"):
        ledger.apply_fill(
            fill("too-large", Side.BUY, 100, "100", "20", "0", CALENDAR[0]),
            CALENDAR[0],
            CALENDAR,
        )
    with pytest.raises(AccountingInvariantError, match="short"):
        ledger.apply_fill(
            fill("short", Side.SELL, 1, "100", "20", "0", CALENDAR[0]),
            CALENDAR[0],
            CALENDAR,
        )
    clean = PortfolioLedger("tamper", Decimal("1000"), SettlementPolicy(), at(CALENDAR[0], 8))
    opening = clean.transactions[0]
    clean.transactions[0] = replace(
        opening,
        postings=(
            Posting("cash", Decimal("999")),
            Posting("contributed_capital", Decimal("-999")),
        ),
    )
    with pytest.raises(AccountingInvariantError, match="cash"):
        clean.reconcile()


def test_dividend_and_integer_stock_split_accounting() -> None:
    ledger = PortfolioLedger("actions", Decimal("20000"), SettlementPolicy(), at(CALENDAR[0], 8))
    ledger.apply_fill(
        fill("buy", Side.BUY, 100, "100", "20", "0", CALENDAR[0]),
        CALENDAR[0],
        CALENDAR,
    )
    ledger.process_settlements(date(2024, 1, 9), at(date(2024, 1, 9), 8, 30))
    dividend = CashDividend(
        "div-1",
        INSTRUMENT,
        date(2024, 1, 10),
        date(2024, 1, 10),
        date(2024, 1, 11),
        Decimal("2"),
        at(date(2024, 1, 8), 18),
        "fixture-dividend-v1",
    )
    assert ledger.accrue_dividend(dividend, at(date(2024, 1, 10), 8, 30)) == Decimal("200")
    split = StockSplit(
        "split-1",
        INSTRUMENT,
        date(2024, 1, 10),
        Decimal("2"),
        at(date(2024, 1, 8), 18),
        "fixture-split-v1",
    )
    assert ledger.apply_split(split, at(date(2024, 1, 10), 8, 31)) == (100, 200)
    assert ledger.positions[INSTRUMENT.key].average_cost == Decimal("50.1")
    processed = ledger.process_settlements(
        date(2024, 1, 11), at(date(2024, 1, 11), 8, 30)
    )
    assert "dividend:div-1" in processed
    assert ledger.settled_cash == Decimal("10180")
    ledger.reconcile()


def test_fractional_split_fails_closed() -> None:
    ledger = PortfolioLedger("split", Decimal("20000"), SettlementPolicy(), at(CALENDAR[0], 8))
    ledger.apply_fill(
        fill("buy", Side.BUY, 100, "100", "20", "0", CALENDAR[0]),
        CALENDAR[0],
        CALENDAR,
    )
    action = StockSplit(
        "fractional",
        INSTRUMENT,
        date(2024, 1, 8),
        Decimal("1.005"),
        at(date(2024, 1, 5), 18),
        "fixture-split-v1",
    )
    with pytest.raises(AccountingInvariantError, match="fractional"):
        ledger.apply_split(action, at(date(2024, 1, 8), 8, 30))


def test_conservative_fill_assumptions_and_partial_fill() -> None:
    simulator = ConservativeFillSimulator(
        FillPolicy(slippage_bps=Decimal("10"), maximum_volume_participation=Decimal("0.1")),
        FeeTaxPolicy(),
    )
    order = intent(Side.BUY, 20)
    base = ExecutionQuote(INSTRUMENT, at(CALENDAR[0], 9), Decimal("100"), Decimal("101"), 100)
    outcome = simulator.simulate(order, base, run_id="run")
    assert outcome.fill is not None
    assert outcome.fill.quantity == 10
    assert outcome.fill.price == Decimal("100.5")
    assert outcome.reason == "partial_fill"
    for quote, reason in (
        (replace(base, suspended=True), "suspended"),
        (replace(base, limit_locked=True), "price_limit_locked"),
        (replace(base, volume=0), "zero_volume"),
    ):
        result = simulator.simulate(order, quote, run_id="run")
        assert result.fill is None and result.reason == reason


def test_pretrade_risk_rejects_cash_position_notional_and_short_breaches() -> None:
    ledger = PortfolioLedger("risk", Decimal("1000"), SettlementPolicy(), at(CALENDAR[0], 8))
    risk = PreTradeRiskEngine(
        RiskPolicy(
            maximum_position_weight=Decimal("0.4"), maximum_order_notional=Decimal("500")
        ),
        FeeTaxPolicy(),
    )
    decision = risk.evaluate(
        intent(Side.BUY, 10),
        estimated_price=Decimal("100"),
        ledger=ledger,
        net_asset_value=Decimal("1000"),
        evaluated_at=at(CALENDAR[0], 9),
    )
    assert decision.outcome is RiskOutcome.REJECT
    assert {
        "maximum_order_notional_exceeded",
        "maximum_position_weight_exceeded",
        "insufficient_available_cash",
    }.issubset(decision.reasons)
    short = risk.evaluate(
        intent(Side.SELL, 1),
        estimated_price=Decimal("100"),
        ledger=ledger,
        net_asset_value=Decimal("1000"),
        evaluated_at=at(CALENDAR[0], 9),
    )
    assert "short_position_forbidden" in short.reasons


def test_strategy_fixtures_and_prediction_availability_contract() -> None:
    fixture = synthetic_backtest_fixture()
    session = fixture.sessions[0]
    buy_hold = BuyAndHoldFixtureStrategy(fixture.instruments[0], session.trade_date)
    assert len(buy_hold.targets(session)) == 3
    assert buy_hold.targets(fixture.sessions[1]) == ()
    equal = EqualWeightFixtureStrategy().targets(session)
    assert sum((target.target_weight for target in equal), Decimal("0")) == Decimal("1")
    top = PredictionTopKFixtureStrategy(2).targets(session)
    assert sum(target.target_weight for target in top) == Decimal("1")
    future_prediction = PredictionObservation(
        fixture.instruments[0], Decimal("1"), session.execution_time, "future"
    )
    with pytest.raises(ValueError, match="future prediction"):
        BacktestSession(
            session.trade_date,
            session.decision_time,
            session.execution_time,
            session.close_time,
            session.quotes,
            session.decision_prices,
            (future_prediction,),
        )


@pytest.mark.parametrize(
    "strategy_factory",
    [
        lambda fixture: BuyAndHoldFixtureStrategy(
            fixture.instruments[0], fixture.sessions[0].trade_date
        ),
        lambda fixture: EqualWeightFixtureStrategy(),
        lambda fixture: PredictionTopKFixtureStrategy(2),
    ],
)
def test_engine_replay_and_exploratory_report_are_deterministic(strategy_factory: object) -> None:
    fixture = synthetic_backtest_fixture()
    strategy = strategy_factory(fixture)  # type: ignore[operator]
    first = EventDrivenBacktestEngine().run(fixture.config, fixture.sessions, strategy)
    second = EventDrivenBacktestEngine().run(fixture.config, fixture.sessions, strategy)
    assert first.events == second.events
    assert first.event_checksum == second.event_checksum
    assert first.journal_checksum == second.journal_checksum
    replay = replay_events(first.events)
    assert replay.event_checksum == first.event_checksum
    assert replay.journal_checksum == first.journal_checksum
    assert replay.final_snapshot_checksum == first.final_snapshot.checksum
    assert replay.snapshot_count == len(first.snapshots)
    report = build_backtest_report(first)
    assert report.status == "exploratory"
    assert report.warning == SYNTHETIC_WARNING
    assert report.reconciliation_status == "passed"
    assert report.event_checksum == first.event_checksum


def test_replay_rejects_tampered_event() -> None:
    fixture = synthetic_backtest_fixture()
    result = EventDrivenBacktestEngine().run(
        fixture.config, fixture.sessions, EqualWeightFixtureStrategy()
    )
    events = list(result.events)
    events[-1] = replace(events[-1], payload={**events[-1].payload, "fills": 999})
    with pytest.raises(ReplayMismatchError, match="identity mismatch"):
        replay_events(tuple(events))


def test_engine_corporate_actions_are_replayable() -> None:
    fixture = synthetic_backtest_fixture("corporate-actions-run")
    instrument = fixture.instruments[0]
    dividend = CashDividend(
        "fixture-dividend",
        instrument,
        fixture.sessions[1].trade_date,
        fixture.sessions[1].trade_date,
        fixture.config.trading_calendar[3],
        Decimal("1.5"),
        fixture.sessions[0].close_time,
        "fixture-dividend-v1",
    )
    split = StockSplit(
        "fixture-split",
        instrument,
        fixture.sessions[2].trade_date,
        Decimal("2"),
        fixture.sessions[0].close_time,
        "fixture-split-v1",
    )
    strategy = BuyAndHoldFixtureStrategy(instrument, fixture.sessions[0].trade_date)
    action_config = replace(
        fixture.config,
        fill_policy=FillPolicy(
            slippage_bps=Decimal("0"), maximum_volume_participation=Decimal("1")
        ),
    )
    result = EventDrivenBacktestEngine().run(
        action_config, fixture.sessions, strategy, (dividend, split)
    )
    action_events = [
        event
        for event in result.events
        if event.kind is BacktestEventKind.CORPORATE_ACTION_APPLIED
    ]
    assert [event.payload["action_type"] for event in action_events] == [
        "cash_dividend",
        "stock_split",
    ]
    assert result.final_snapshot.dividends > 0
    assert replay_events(result.events).final_snapshot_checksum == result.final_snapshot.checksum
