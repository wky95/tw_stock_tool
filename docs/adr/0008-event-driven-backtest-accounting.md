# ADR 0008: Deterministic event-driven backtest and accounting core

- Status: accepted for Phase 2 Slice 1
- Scope: offline research backtests for long-only Taiwan ordinary equities

## Decision

The backtest is a deterministic event processor. Inputs are pinned sessions, decision-time
prices, open/close execution quotes, predictions with `available_at`, corporate actions, a
trading calendar, and versioned policies. The engine emits immutable, typed events with a
schema version, monotonic sequence, timezone-aware timestamp, deterministic ID, correlation
ID, causation ID, and a canonical payload. Replaying an altered, incomplete, out-of-order, or
internally inconsistent stream fails closed.

The event flow for each session is:

```text
run start
  -> prior-session-close target positions
  -> integer-share order intents
  -> TWD settlement / dividend payment
  -> corporate actions
  -> market-open marks
  -> pre-trade risk decisions
  -> cash / sell-quantity reservation
  -> conservative fills or explicit unfilled outcomes
  -> reservation adjustment / terminal release
  -> market-close marks
  -> accounting snapshot
  -> reconciliation
  -> final T+2 settlement(s)
  -> run complete
```

Strategies can only emit target positions. The fixture strategies are synthetic buy-and-hold,
equal-weight, and prediction top-k. They do not submit orders or mutate accounting state.

## Ledger and reconciliation

Postings use positive debits and negative credits and every transaction must satisfy:

```text
sum(posting.amount) = 0
NAV = settled cash + settlement receivables - settlement payables
      + dividend receivables + marked position value
available cash = settled cash - unsettled buy payables
average cost = exact position book cost / quantity
unrealized PnL = marked position value - exact position book cost
realized PnL = net sale proceeds - allocated exact book cost
```

Exact book cost is authoritative; average cost is derived after every trade. Reconciliation
also checks journal balances against cash, receivables, payables, inventory, fees, taxes,
dividends, and realized PnL. Positions must remain non-negative integer shares. Any mismatch
raises `AccountingInvariantError`; replay mismatches raise `ReplayMismatchError`.
All monetary state uses `Decimal`; balance and residual checks require exact zero without an
epsilon. Buy reservations, unsettled buy payables, and unsettled sale receivables are distinct.
Sale receivables do not increase buying power.

## Versioned assumptions

- `tw-cash-equity-costs-engineering-fixture-v2`: commission is rounded up to whole TWD and its
  minimum applies once per order lifecycle, including same-day or cross-day partial fills;
  transaction tax applies to each sell fill and is rounded down to whole TWD. It is explicitly
  not a verified universal Taiwan broker tariff.
- `tw-cash-equity-gross-t-plus-2-engineering-fixture-v2`: settlement is T+2 pinned open
  sessions in TWD. Receivables and payables remain separate gross obligations; missing future
  sessions fail closed and the manifest records sessions used only for final settlement.
- `conservative-open-fill-v1`: execution starts from the next-session open, adds adverse
  configurable slippage, rounds against the order to the instrument tick, caps participation,
  permits configured partial fills, and leaves suspended, limit-locked, or zero-volume orders
  unfilled.
- `tw-cash-equity-tick-schedule-engineering-fixture-v1`: adverse prices are iteratively rounded
  using the tick for the final price band so crossing a band cannot produce an illegal tick.
- `long-only-cash-risk-v1`: TWD ordinary equity only, no shorting, leverage, margin, or negative
  available cash; pending reservations participate in cash, sellable quantity, projected
  position weight, order notional, and gross exposure checks.
- `last-valid-close-mark-engineering-fixture-v1`: a missing daily close carries the last valid
  mark with timestamp, source, and stale age. Exceeding the configured stale-session limit
  fails closed; incomplete valuation cannot be promoted.
- `cash-dividend-accounting-v1` and `stock-split-accounting-v1`: dividends accrue for held
  entitled quantity on ex-date and payment only transfers the existing receivable to cash.
  Announcement, ex, record, payment/effective dates and pinned action versions are required;
  splits preserve exact book cost and reject fractional-share results.

Policy metadata records `jurisdiction=TW`, `asset_type=cash_equity`, fixture purpose, effective
dates, broker specificity, source verification status, and assumptions where applicable. Fees,
tax, odd-lot, day-trading, and broker rules must be verified again before broker integration.

## Reporting boundary

The report always labels included fixtures as synthetic and not evidence of real-world
performance. If point-in-time reference data is incomplete, its status is `exploratory`.
This slice does not connect to the Dashboard, a broker, paper/live trading, authentication,
or multi-currency/derivatives accounting.

Known research limitations include synthetic OHLC/volume fixtures, a deliberately minimal
open-price fill model, no queue modeling, no intraday path, no odd-lot session distinction,
no exchange holiday source beyond the pinned calendar, no withholding tax, and only cash
dividends and integer-preserving splits for corporate actions.
