# ADR 0013: Replayable paper accounting runtime

Status: accepted

Paper accounting is a deterministic projection of the immutable OMS fill journal. The projection
requires a pinned instrument reference and trading calendar, applies versioned engineering-fixture
fee and T+2 settlement policies, uses Decimal strings for every monetary value, and publishes a
content-addressed snapshot to the operations database. Repeating the same projection is idempotent.

Pending buy cash and sell quantity reservations live in the OMS database so risk approval, submit
outbox creation and reservation creation share one ACID transaction. Fill transitions reduce the
reservation atomically; rejection and cancellation release it atomically. The accounting projection
is deliberately a separate database boundary: a crash is repaired by replay, and disagreement with
the OMS source enters safe mode rather than being guessed away.

`paper-service-run --paper --once` executes one pinned session under the versioned scheduler. With no
strategy adapter it explicitly produces no targets or new orders; it may safely drain an existing
paper outbox. An external supervisor may invoke one-cycle runs, but this ADR does not enable a broker
network connection, live trading or automatic liquidation.
