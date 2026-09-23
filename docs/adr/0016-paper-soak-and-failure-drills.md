# ADR 0016: Multi-session PAPER soak and failure drills

## Status

Accepted for local, offline PAPER engineering verification only.

## Decision

A strict version-1 JSON plan drives multi-session PAPER soak runs. Its canonical bytes determine the
plan SHA-256. The plan pins an ordered trading/settlement calendar, exact instrument-reference
versions, complete timestamped market events for every cycle, optional exact paper-target artifact
versions, and an explicit deterministic broker participation cap. `latest`, missing market coverage,
naive timestamps, unknown fields, invalid checksums, and absent settlement sessions fail closed.

Dry-run validates the plan and all referenced target artifacts without creating operational state.
Confirmed execution reconstructs the PAPER runtime for every market session to exercise restart
behavior. It then repeats the same session against the same state and requires an unchanged state
identity. The runner checks safe mode, order terminality, transactional outbox drainage, cash and
position reservation release, valuation completeness, exact Decimal accounting reconciliation, and
T+2 settlement completion.

The broker participation cap is stored in the content-addressed plan. Lowering it below the strategy
risk cap is an intentional failure injection for partial-fill/restart tests; it is not a model of any
Taiwan broker. A partial order that cannot be reconciled after reconstructing an in-memory broker
must enter safe mode instead of being guessed complete.

Every confirmed run publishes an immutable `paper_soak_reports` artifact. Passed reports are
`validated`; failed reports remain available as `incomplete` diagnostics. Both use the
`paper_engineering_soak` classification and explicitly deny live-performance meaning.

## Consequences and limits

- Restart, duplicate-session replay, stale-data, missing-calendar, and partial-fill uncertainty are
  exercised without network access.
- Determinism depends on exact local artifacts, plan bytes, and versioned engineering policies.
- The deterministic broker does not model an order book, queue priority, connectivity, or actual
  broker behavior.
- This does not provide credentials, authentication, external scheduling, a real broker adapter,
  paper/live network transport, or authorization to trade.
