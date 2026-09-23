# Paper Operations runbook

This service is PAPER-only. It has no live broker adapter, credentials, public deployment, or order
control in the Dashboard.

## Startup

1. Confirm `trading.live_enabled=false` and all state paths point to the paper namespace.
2. Run `island-quant paper-init --paper` and `island-quant paper-reconcile --paper`.
3. Start `island-quant dashboard --paper-operations`; it binds to `127.0.0.1` by default.
4. Readiness requires OMS integrity, startup reconciliation, a fresh heartbeat, and safe mode off.

Run one pinned, idempotent operations cycle:

```bash
island-quant paper-service-run --paper --once \
  --session 2025-01-02 --as-of 2025-01-02T09:00:00+08:00 --dry-run
island-quant paper-service-run --paper --once \
  --session 2025-01-02 --as-of 2025-01-02T09:00:00+08:00
```

The default files are deterministic engineering fixtures. Replace them only with pinned, validated
paper inputs. A missing instrument reference, calendar session, fresh market event, complete mark or
reconcilable fill causes the run to fail closed.

An optional strategy run must name one exact validated paper-candidate version; `latest` and
exploratory artifacts are rejected:

```bash
island-quant paper-service-run --paper --once \
  --session 2025-01-02 --as-of 2025-01-02T09:00:00+08:00 \
  --target-artifact-version <exact-sha256> --dry-run
```

Dry-run verifies the artifact contract without creating OMS state. Before removing `--dry-run`,
confirm its session, decision/availability times, complete held-position coverage, long-only weights,
pinned instrument/calendar lineage, and execution prices. The normal run stores research lineage
with each order and in the daily report. A rejected target is not manually bypassed; publish a new
immutable corrected artifact version.

Promote only a separately validated research target. Start with the same command using `--dry-run`,
then repeat with `--confirm` only after reviewing every field:

```bash
island-quant paper-promote-target --paper \
  --source-version <exact-research-target-sha256> \
  --reviewer local-operator --reason "offline review passed" \
  --approved-at 2025-01-01T18:00:00+08:00 --execution-session 2025-01-02 \
  --approve-pit --approve-data-license --approve-risk --dry-run
```

The approval is an immutable local audit record, not authentication or a digital signature. Never
promote an exploratory result by changing its manifest. Produce a new validated source artifact
after resolving coverage, licensing, PIT, model, valuation, and risk blockers.

Before execution, review configured minimum cash buffer, turnover, market-volume participation,
position-count, gross-exposure, and concentration limits. A missing pinned volume or incomplete
valuation blocks new OMS writes. The Dashboard target/actual table is read-only and reports drift
only when the reconciled portfolio valuation is complete.

Jobs use exact job versions, a pinned trading calendar session, a singleton lease, deterministic run
keys, bounded retries and dead-letter state. A completed decision session is never run again.

## Multi-session soak and restart drill

Run an offline soak before treating a PAPER configuration as operationally exercised:

```bash
island-quant paper-soak-run --paper --plan <paper-soak-plan.json> --dry-run
island-quant paper-soak-run --paper --plan <paper-soak-plan.json> --confirm
```

The strict schema-version-1 plan pins an ordered settlement calendar, instrument ID/market/reference
version, and one or more ordered cycles. Every cycle contains a timezone-aware `as_of`, complete
market coverage, an optional exact `paper_target_snapshots` SHA-256, and a decimal
`broker_participation_cap` in `(0, 1]`. The cap is a deterministic engineering failure-injection
control. It is not a statement about a broker, exchange liquidity, or expected execution.

Dry-run validates the complete plan and every referenced target without creating SQLite databases or
reports. Confirmed execution reconstructs the scheduler, broker, accounting projector, and runtime
for each session; repeats the same session to test idempotency; and checks terminal order state,
drained outbox, released reservations, complete valuation, exact accounting reconciliation, and
final settlement. A buy needs enough pinned future sessions for T+2; the runner never invents a
settlement date.

The runner uses the separately configured `paper.soak_*_database_path` values. Configuration
validation rejects any overlap with the normal PAPER OMS, operations, or scheduler databases. Keep
the soak namespace disposable and preserve it only when investigating a failed report.

The final `paper_soak_reports` artifact is content-addressed, pins the plan and target lineage, and is
classified `paper_engineering_soak`. A failed drill is still published with completeness
`incomplete` for diagnosis and the CLI exits non-zero. Preserve that report and databases when
investigating; do not edit state or change the plan in place. A corrected plan has a new hash.

Soak is fully local and deterministic. It neither connects to a broker nor establishes execution
quality, market realism, alpha, or readiness for live trading.

## Safe mode and recovery

Critical reconciliation, stale data/heartbeat, risk limits, repeated failures, corruption or a crash
loop stops new risk. It may cancel pending paper orders but never auto-liquidates. Inspect the OMS
journal, outbox, reconciliation and alerts. Do not edit SQLite directly. Manual resume requires a
specific operator reason; unknown orders require `paper-recover --paper --dry-run` followed by
`--confirm` only after the broker fixture state is known.

For a locked database, stop duplicate local processes and retry. For corruption, preserve the file
for diagnosis and restore a known paper backup; do not guess missing state. Dead-letter jobs require
an explicit investigation and a new version or recovery decision.

## Shutdown

Stop accepting new work, allow the active transaction to finish, persist the heartbeat, and inspect
pending outbox work. Shutdown with pending work enters safe mode so restart recovery reconciles
before resuming.
