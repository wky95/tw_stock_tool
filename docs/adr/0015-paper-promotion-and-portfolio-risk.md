# ADR 0015: Audited PAPER promotion and portfolio risk

## Status

Accepted for the local, offline PAPER environment only.

## Decision

Research targets and executable paper targets use separate content-addressed namespaces. A source in
`research_target_candidates` must be schema-valid, checksum-valid, complete, classified
`validated_research_candidate`, and pin all required strategy, model, prediction, dataset, universe,
policy, instrument-reference, and calendar versions. Exploratory artifacts are never promoted.

Promotion requires an exact source SHA-256 plus an identified reviewer, reason, timezone-aware
approval timestamp, execution session, and explicit PIT, data-license, and risk checklist approvals.
Dry-run performs every validation without writing. Confirmed promotion first publishes an immutable
`paper_promotion_approvals` audit artifact, then publishes `paper_target_snapshots` with the approval
version added to lineage. Both writes are content-addressed and rerunning the same request is
idempotent. Approval must precede the execution session. There is no `latest` lookup or automatic
promotion from exploratory results.

The paper runtime persists its selected target snapshot in operational state without deleting the
immutable artifact. The pointer is cleared when the runtime is explicitly started without a
strategy. The read-only Paper Operations Dashboard compares each target with the reconciled marked
position and reports target weight, actual weight, absolute drift, quantity, mark, decision time,
and exact target version.

`paper-target-risk-v1` adds configurable minimum cash buffer, maximum turnover, maximum market-volume
participation, and maximum position count to the existing long-only gross exposure, concentration,
cash reservation, and integer-share rules. All checks finish before any OMS order is written. Missing
or non-positive pinned volume fails closed. Unsettled sales still provide no buying power.

## Limits

- Promotion is an explicit local operator workflow, not an authenticated multi-user approval system
  or cryptographic signature service.
- This milestone defines the validated source contract but does not claim existing exploratory
  pipeline outputs satisfy it.
- Target/actual drift uses the reconciled portfolio marks and is unavailable when valuation is
  incomplete; it is operational telemetry, not performance attribution.
- Broker rules, liquidity, slippage, fees, taxes, and limits remain engineering policies that must be
  verified before external broker integration.
- No live broker, credentials, network submission, shorting, margin, or public control endpoint is
  introduced.
