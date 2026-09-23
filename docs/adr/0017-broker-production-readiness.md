# ADR 0017: Provider-neutral broker and production readiness boundary

## Status

Accepted for offline readiness work only. This decision does not authorize broker login, network
transport, order submission, deployment, or live trading.

## Decision

Future broker adapters must implement `BrokerSession` from `island_quant.brokers.contracts` and
negotiate an explicit, versioned `BrokerCapabilities` set before any command is eligible. Domain and
OMS code must not import vendor SDK types. Credentials cross this boundary only as an opaque
`CredentialReference` containing a provider, reference name, and account alias; it cannot contain a
secret value.

Submit, cancel, replace, query, health, rate-limit, event, and reconciliation messages are typed.
Order and fill events preserve event, correlation, causation, sequence, business, client-order,
broker-order, and fill identities. The error taxonomy is versioned and pairs every error with a
retry directive. A timeout or transport failure is an unknown outcome and can only yield
`reconcile_then_decide`; unconditional resubmission is invalid.

Every non-healthy session sets `kill_new_risk`. Sequence gaps, corrupt payloads, uncorrelated broker
orders, and reconciliation mismatches also stop new risk. The existing PAPER runtime remains on its
deterministic broker for this milestone; introducing these contracts does not silently change its
behavior.

The `OfflineBrokerFixture` exists solely for adapter contract tests. Its failure injection is not a
model or claim about Shioaji, SinoPac, TWSE, or TPEx behavior.

## Consequences

- A future Shioaji adapter has a narrow integration surface and must map all vendor values at the
  adapter edge.
- Adapter implementation remains blocked on account eligibility, credential handling, official
  semantic verification, production data licensing, deployment design, and an operator-approved
  staged-risk policy.
- The readiness report can improve as evidence arrives without weakening fail-closed defaults.
