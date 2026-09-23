# ADR 0019: Production admission and no-go evaluator

## Status

Accepted for offline decision validation only. It does not authorize provider purchase, credential
use, broker login, deployment, order submission, a control plane, or live trading.

## Decision

One explicitly named, versioned JSON decision/evidence pack is validated by
`island-quant validate-production-readiness --pack <absolute-exact-path>`. The parser rejects unknown
fields, unsupported versions, relative evidence URLs, path traversal, missing owners, bad checksums,
expired evidence and unknown critical decisions. It performs no discovery, network access,
credential resolution or runtime-state writes.

Admission gates cover provider identity and PIT coverage, license/entitlement decisions,
parallel-run reconciliation and rollback, security/deployment decisions, and preservation of all
eight unresolved Shioaji semantic blockers. Critical coverage remains exactly 100%; unexplained
critical differences, stream faults and silent fallback remain exactly zero.

A complete synthetic pack may pass `synthetic_offline` conformance. Its report always says
`production_admission=no_go` and `live_trading_ready=false`. Production admission remains no-go until
real provider, legal and operator evidence is supplied. A schema is evidence structure, not proof
that a control exists.

## Consequences

- Reports are canonical JSON with a deterministic checksum.
- Every blocker names an owner, required evidence and next safe step.
- Broker unknowns cannot be represented as resolved by this schema.
- Provider disagreements require an operator decision; the evaluator never chooses a provider.
- An expired, stale or unlicensed rollback source causes a stop, never fallback.
