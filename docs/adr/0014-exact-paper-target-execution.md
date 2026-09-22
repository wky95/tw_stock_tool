# ADR 0014: Exact-version paper target execution

## Status

Accepted for PAPER-only engineering use. This ADR does not authorize live trading.

## Decision

The paper runtime accepts an optional exact SHA-256 version from the
`paper_target_snapshots` artifact namespace. It never resolves `latest`, scans for a newest
artifact, or falls back to the exploratory pipeline `targets` placeholder. If no version is
provided, the decision path creates no new risk.

An accepted manifest must be checksum-valid, schema version 1, `validated`, and classified as a
`paper_candidate`. It pins strategy, model, prediction, dataset, universe, target-policy,
instrument-reference, and calendar versions. Every lineage value is itself a canonical SHA-256
digest. Records are timezone-aware, available by decision time, assigned to the requested execution
session, eligible, unique, long-only, and backed by a positive pinned execution price.

The target snapshot is a complete desired-position view for every currently held or pending
instrument. Omission of an existing position fails closed instead of silently implying either
"hold" or "liquidate". Integer desired quantities are floored from NAV and target weight. Pending
orders are included in projected quantities, and deterministic idempotency keys prevent duplicate
risk on replay.

The versioned `paper-target-risk-v1` fixture limits gross exposure, individual position weight,
orders per session, and estimates adverse buy slippage before the OMS cash reservation. The OMS
continues to reserve settled/available cash and owned sell quantity atomically with the outbox.
Unsettled sale proceeds are not added to buying power. Sells are queued before buys but do not fund
those buys.

Order lineage is written in the same SQLite transaction as order creation and includes the target
artifact and all upstream research versions. The immutable daily report records both target artifact
and portfolio projection versions. Any artifact, risk, OMS, replay, valuation, or reconciliation
failure enters the existing fail-closed runtime path.

## Limits

- The artifact producer is still an offline engineering interface; no automated promotion workflow
  signs a research result as a paper candidate.
- The deterministic paper broker is a fill fixture, not a claim about Taiwan broker execution.
- Fees, taxes, settlement, slippage, and limits remain explicitly versioned engineering fixtures and
  require verification before any broker integration.
- There is no broker connection, credential handling, paper/live network transport, authentication,
  shorting, leverage, or live-control endpoint.
