# ADR 0009: Prediction-to-backtest analytics and immutable artifacts

Status: accepted for Phase 2 Slice 2 (candidate research only)

## Decision

Only a selected, exact-version OOS prediction ledger may enter a backtest. Each row pins its
experiment, fold, model, features, label, target definition, availability, fit cutoff, role,
completeness, and checksum. Train rows are forbidden. Validation rows are engineering-only and
mark the result exploratory. Holdout rows require an explicit access audit. Eligibility and
listing boundaries are checked at decision time. A target policy materializes immutable target
positions before the existing risk and execution layers see an order intent.

Top-K sorts by descending prediction then ascending instrument ID. Quantile selection uses the
same ordering at its boundary. Threshold selection is strict (`prediction > threshold`). Invalid
observations never enter a rank. Weights are equal, long-only, reduced by the configured cash
buffer, and capped per position. Daily, weekly, and N-session schedules operate only on the pinned
trading calendar. Missing-row handling is versioned independently from a decision-date coverage
threshold. The default requires at least 80% valid coverage and fails closed below it; an explicit
policy may skip the entire rebalance. Above that threshold, an isolated missing/invalid row may be
liquidated with a typed `data_quality:*` reason. Reports separate strategy turnover from
data-quality turnover and disclose forced-liquidation count and costs. `skip_rebalance` suppresses
the whole rebalance when an expected artifact row is absent; `hold_previous_target` applies the
available targets while leaving the missing instrument unchanged.

## Timing and execution

A target generated after session `t` closes can first execute at session `t+1` open. Fill events
retain the causal order, reference price, reference source, execution price, tick policy, fee
policy, and fill policy. Slippage is never inferred from a future price:

```text
buy slippage  = (execution - reference) × quantity
sell slippage = (reference - execution) × quantity
```

A positive value is a cost. Tick rounding is already present in execution price and is therefore
not counted a second time.

## Performance definitions

NAV returns are session-to-session, including the initial capital as the first denominator:

```text
r_t = NAV_t / NAV_(t-1) - 1
total return = ending NAV / initial NAV - 1
CAGR = (ending NAV / initial NAV)^(365.25 / elapsed calendar days) - 1
annual volatility = sample_stdev(r_t) × sqrt(A)
Sharpe = (mean(r_t) - annual risk-free / A) / sample_stdev(r_t) × sqrt(A)
Sortino = (mean(r_t) - annual MAR / A) / downside_deviation × sqrt(A)
```

Return frequency is one pinned trading session. `A`, annual risk-free rate, annual MAR, and the
minimum observation count are versioned report inputs. The per-session risk-free rate is
`annual_rate / A`. Volatility defaults to sample standard deviation with `ddof=1`; the report pins
the selected `ddof`, and the implementation supports only 0 or 1. CAGR uses actual elapsed
calendar days with a 365.25-day exponent, including irregular gaps, while volatility and risk
ratios use trading-session annualization. CAGR requires elapsed time and positive endpoint NAV.
Sharpe, Sortino, beta, alpha, tracking error, and
information ratio remain unavailable when the sample or denominator is insufficient. External
cash flows are unsupported and fail closed. Drawdown is `NAV_t / running_peak_t - 1`; start is the
peak owning the maximum trough, recovery is its first later regain, and an unrecovered episode is
explicitly ongoing. Synthetic metrics are engineering checks, not statistical evidence.

Turnover is total executed gross notional divided by average sampled NAV. Exposure is marked
position value divided by NAV at session close. Cash weight is available TWD cash divided by NAV
at session close. All ledger values and
reconciliation amounts remain `Decimal`; statistical ratios use floating point only after return
construction and never feed accounting.

## Benchmark handling

TAIEX, TPEx, and PIT eligible-universe market-cap benchmarks have distinct identities and exact
dataset versions. Benchmark sessions must exactly equal strategy sessions. No future backfill is
allowed. The initial benchmark level is explicit so the first session return has the same boundary
as the strategy. A PIT market-cap benchmark fails closed if contemporaneous market caps are
incomplete. Alpha and beta are emitted only with sufficient observations and non-zero benchmark
variance.

## Attribution identity

The mutually exclusive portfolio identity is:

```text
ΔNAV = gross price movement + dividends - brokerage fees - transaction taxes
       - slippage + corporate-action effects + external flows
```

Realized PnL and ending unrealized PnL are memo disclosures, not additional additive components;
adding them beside price movement would double count. The same identity is checked by session.
Gross price movement is reconstructed independently from ending marked position value, cumulative
gross buy/sell cash flows, and measured slippage; it is not obtained by solving the identity for a
residual. Known cash-dividend and integer-preserving stock-split events are accepted. Any unknown
corporate-action type fails closed instead of entering a residual or ambiguous bucket.
The schema name `notional_allocated_attribution` allocates the reconciled portfolio result by
executed notional only so the rows add up. It is not security-level economic attribution, does not
represent a security's real price/dividend/cost contribution, and must not support security
selection conclusions. Every additive row must have an exact zero residual. Realized and
unrealized PnL remain memo fields and are never additional identity terms. A residual is never used
to hide a defect.

## Scenarios and artifacts

The deterministic scenario runner saves completed and failed runs for all configured slippage,
participation, fee, tax, rebalance, and Top-K settings. Each evaluator receives immutable pinned
inputs and creates a fresh engine, ledger, reservations, and event journal. No test performance is
used to select or recommend a configuration. Scenario IDs are canonical configuration digests;
duplicates are rejected, failures retain a typed reason code, execution order cannot select a
winner, and the default maximum grid is 1,000 scenarios unless an explicit override is supplied.
The complete grid identity is included in the manifest before execution. Dry-run reports count and
estimated engine runs without evaluating the grid.

Backtest artifacts are canonical-JSON, content-addressed, immutable candidates. Their manifest
pins all upstream versions, policy versions, code provenance, configuration and input checksums,
event/ledger/snapshot/report checksums, seed, time range, initial capital, completeness, and
synthetic status. `created_time` is instance metadata and is excluded from the content identity;
PID, host, duration, filesystem paths, and temporary roots are never identity inputs. Map ordering
is canonicalized. Readers accept only a 64-character lowercase SHA-256 identity and constrain its
resolved directory to the configured root. A candidate directory is fully written, checksummed,
fsynced, verified, and atomically renamed. Concurrent publishers use directory-rename CAS;
identical content is idempotent and a conflicting identity fails closed. Readers ignore partial
candidate directories and reject missing files, symlinks, checksum damage, and incomplete
manifests. Event, performance, final-snapshot, and scenario-inventory checksums are verified again
on read. `latest` and `current` are invalid. Promotion is deliberately unavailable in this
slice and always rejects; dirty code, incomplete PIT data, synthetic input, or non-zero
reconciliation are independently disqualifying.

The Dashboard adapter reads one exact artifact through the store, removes path- and secret-like
keys, and exposes GET-only projections. It cannot fall back to demo mode. Templates never access
the filesystem. Demo mode retains its separate empty state.

## Consequences and limits

This slice provides a reproducible research loop and transparent candidate reports. It does not
provide licensed real benchmark data, complete PIT reference data, broker-specific costs,
security-level economic attribution, external-flow accounting, paper/live execution, order
submission, tick data, queue simulation, shorts, leverage, derivatives, or strategy optimization.
