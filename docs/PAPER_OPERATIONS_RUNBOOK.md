# Paper Operations runbook

This service is PAPER-only. It has no live broker adapter, credentials, public deployment, or order
control in the Dashboard.

## Startup

1. Confirm `trading.live_enabled=false` and all state paths point to the paper namespace.
2. Run `island-quant paper-init --paper` and `island-quant paper-reconcile --paper`.
3. Start `island-quant dashboard --paper-operations`; it binds to `127.0.0.1` by default.
4. Readiness requires OMS integrity, startup reconciliation, a fresh heartbeat, and safe mode off.

Jobs use exact job versions, a pinned trading calendar session, a singleton lease, deterministic run
keys, bounded retries and dead-letter state. A completed decision session is never run again.

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
