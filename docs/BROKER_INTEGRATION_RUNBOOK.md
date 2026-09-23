# Broker integration readiness runbook

This runbook is a design and audit artifact. No external broker adapter exists, no credential is
accepted by application configuration, and live trading remains unavailable.

## Current safety boundary

1. Keep `trading.mode` non-live and `trading.live_enabled=false`.
2. Never put API keys, secret keys, certificate passwords, certificate bytes, personal IDs, or
   account numbers in YAML, logs, artifacts, fixtures, tests, or Git.
3. Treat `CredentialReference` as a locator only. A future composition root may resolve it from a
   macOS Keychain, environment injection, or an approved secret manager and must not return the
   value to domain code.
4. Do not retry submit after timeout, disconnect, or lost response. Set kill-new-risk, query and
   reconcile by stable identities, then require a deterministic recovery decision.
5. Dashboard and health endpoints remain read-only. No login, submit, cancel, live toggle, or kill
   switch control endpoint may be added.

## Required startup sequence for a future adapter

The following sequence is intentionally not implemented yet:

1. Verify a two-step live enablement approval, deployment identity, clean immutable release, clock
   synchronization, network allowlist, and exclusive process lease.
2. Resolve credentials outside the domain; verify rotation state and certificate expiry without
   logging values.
3. Open exactly one session, pin adapter/SDK versions, negotiate capabilities, and verify account
   eligibility is the approved cash-stock account.
4. Establish order/deal subscriptions and a backend reconciliation baseline. A missing baseline,
   sequence gap, pending/untrackable report, or projection failure keeps kill-new-risk active.
5. Reconcile orders, fills, positions, cash, pending settlements, and reservations. Differences are
   investigated; they are never automatically repaired.
6. Run pre-trade risk self-tests with production instrument, calendar, price-limit, suspension,
   entitlement, and licensed market-data inputs.
7. Only after an operator reviews all gates may a separately authenticated control plane admit the
   staged-capital limit. The local Dashboard is not that control plane.

## Failure handling

- **Unknown submit/cancel/replace outcome:** retain the original idempotency and correlation
  identities, stop new risk, query orders, refresh the broker's daily state, compare fills, and do
  not resend unless absence is authoritative and the recovery policy explicitly permits it.
- **Sequence gap/out-of-order/duplicate event:** deduplicate by business and fill identity, buffer or
  reconcile gaps, and never infer a fill from order state alone.
- **Session expiry/reconnect:** stop new risk during the gap. Re-establish subscriptions and a full
  reconciliation baseline before resuming.
- **Rate limit:** back off within a local budget. Never use aggressive polling as a recovery loop.
- **Cancel/replace versus fill race:** broker fills are facts. Reconcile cumulative filled quantity
  before changing reservation or accounting state.
- **Corrupt or unmappable payload:** preserve redacted evidence, alert, and stop. Do not coerce an
  unknown vendor value into the nearest domain enum.
- **Disaster recovery:** restore an immutable application build and verified database backup into an
  isolated process, then reconcile to the broker before permitting new risk.

## Deployment and security requirements

- Dedicated non-admin process identity; least-privilege filesystem and outbound-only broker network
  allowlist; no public inbound port.
- macOS Keychain for a single local operator or an approved secret manager for a managed host;
  environment variables may transport values only through a supervised ephemeral process and must
  not be inherited by unrelated children.
- Rotation and revocation drill for API key, secret key, and certificate; automatic expiry alerts;
  logs prove which reference/version was used without containing the value.
- Authenticated, authorized, CSRF-resistant control plane separated from the read-only Dashboard;
  immutable audit events for every approval and control action.
- Encrypted, tested backups; documented RPO/RTO; UTC/NTP monitoring; bounded audit retention that
  also satisfies broker, exchange, data-license, and legal requirements.
- Independent manual kill switch that blocks new risk and optionally requests cancels but never
  auto-liquidates.

## Rollout and rollback

Start with read-only authenticated queries, then simulation conformance, then a no-submit production
shadow session. A later milestone may propose a separately approved minimum-capital/capped-order
trial. Roll back immediately on an identity mismatch, unknown outcome that cannot be reconciled,
stale or unlicensed data, clock drift, session degradation, risk-gate disagreement, unexpected
position/cash/settlement, missing audit evidence, or operator uncertainty.

## Go/no-go before writing a Shioaji adapter

All items must be **go**:

- [ ] Operator selects an eligible SinoPac cash-stock account and confirms signed/API-test status.
- [ ] Operator selects secret backend, rotation owner, and certificate lifecycle procedure.
- [ ] Exact Shioaji version is pinned and its wheel is tested on the deployment macOS arm64/Python.
- [ ] Official semantics for stable client identity, lost response, event ordering, reconnect,
      cancel/replace races, error codes, and retention are resolved or conservatively mapped.
- [ ] Licensed production provider and rights cover real-time/non-display use, storage, derived data,
      audit retention, backups, and any display/redistribution.
- [ ] PIT listing, delisting, corporate action, announcement-time, suspension, price-limit, calendar,
      unexpected-closure, benchmark, and market-cap feeds pass coverage tests.
- [ ] Authenticated control plane, two-step enablement, process isolation, backup/restore, clock,
      network allowlist, monitoring, manual kill switch, staged-capital limits, and rollback owner are
      approved and drilled.
- [ ] Offline conformance, PAPER soak, Linux/arm64 build, and target deployment build are green.

Any unchecked item is **no-go**. Even a fully implemented adapter would not by itself mean the
system is safe for live trading.
