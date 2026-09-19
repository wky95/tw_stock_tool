# ADR 0011: Persistent paper OMS and deterministic broker

Status: accepted

The paper environment uses a storage-neutral OMS domain and a local SQLite adapter. Every order
transition is a typed, versioned, checksummed event. Order state and journal append share one ACID
transaction with optimistic concurrency. Submission uses a transactional outbox and keeps the same
idempotency key across retries. Fills and their state transitions are atomic.

The deterministic paper broker reads pinned market events only. It models latency, rejection,
participation-capped partial fills, suspension, zero volume and adverse slippage. It does not model
exchange queues and must not be interpreted as execution performance.

SQLite state is restricted to `paper`, configured by relative path, ignored by Git, and rejected on
environment mismatch, corruption, lock contention or unsupported schema version. Critical
reconciliation mismatches set `kill_new_risk`; the system never guesses a repair. Manual recovery
requires an explicit reason and confirmation. There is no live broker, credential input, withdrawal,
public endpoint or live CLI flag.
