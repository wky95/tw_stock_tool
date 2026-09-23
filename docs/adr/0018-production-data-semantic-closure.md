# ADR 0018: Production-data contracts and broker semantic closure

## Status

Accepted for offline readiness work only. This decision does not select or purchase a provider,
grant data rights, connect a broker, implement a secret backend or control plane, deploy a service,
or authorize live trading.

## Decision

Production data uses provider-neutral contracts in `island_quant.data.production`. Capabilities and
entitlements are negotiated explicitly. `EntitlementReference` contains names only. Point-in-time
instrument identity, identifier validity, corporate-action revisions, tradability, official
calendar changes, benchmark constituents, market cap, streaming sequence/freshness, retention and
coverage are first-class records. Domain code must not import paid-provider types.

Every critical production dataset requires complete fields and records for the admitted decision
scope. Missing timestamps, unknown tradability, stale streams, sequence gaps, expired entitlement,
retention violations, unexplained provider disagreement, and incomplete PIT coverage fail closed.
An alternate provider can be selected only by a versioned operator decision; silent fallback is
forbidden.

The offline conformance harness uses synthetic fixtures and makes no assertion about a real
provider. It exercises late and revised announcements, listing boundaries, identifier changes,
suspension, price-limit exceptions, emergency closures, benchmark omissions, stream faults,
entitlement and retention, disagreement and fallback.

Official Shioaji evidence narrows the reconnect baseline to `update_status` plus cache health for
the current day's orders. It does not close cross-day identity scope, authoritative negative lookup,
race atomicity, retention horizon, token expiry, complete error mapping or compatibility policy.
Those remain blockers; no incompatible semantics are selected.

Security work is also contract-only: opaque secret resolution, rotation evidence, least-privilege
process/filesystem/network policy, authenticated control-plane threat model, audit/backup policy,
clock drift and independent two-step staged-capital approval. No backend or enablement path exists.

## Consequences

- A licensed adapter can be evaluated without changing domain types.
- Critical coverage has a deliberately strict 100% acceptance threshold; an exception requires a
  new reviewed ADR, not a runtime fallback.
- Procurement and legal answers are deployment inputs, not assumptions embedded in code.
- Phase 4B improves readiness evidence but the system remains no-go for live trading.
