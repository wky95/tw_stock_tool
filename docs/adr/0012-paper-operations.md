# ADR 0012: Recoverable paper operations

Status: accepted

Paper operations use a pinned-calendar scheduler with versioned idempotency keys, a singleton lease,
bounded retry/backoff, misfire/catch-up policy and persistent dead letters. A clock protocol makes
time behavior deterministic in tests.

Service lifecycle is fail closed: startup verifies persistent OMS state and reconciliation;
heartbeat staleness, crash loops and critical mismatches enter safe mode. Manual resume requires a
reason. Monitoring and transport-neutral alerts persist deduplication, cooldown, acknowledgement and
escalation state. The local JSONL alert sink is a fixture and sends nothing externally.

The Paper Operations Dashboard is read-only and mode-isolated. Daily reports are immutable,
content-addressed and explicitly paper. Kill-new-risk never auto-liquidates. No component provides a
live toggle, broker network access, credentials, withdrawal or public deployment.
