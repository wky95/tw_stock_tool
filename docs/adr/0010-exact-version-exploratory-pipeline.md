# ADR 0010: Exact-version real exploratory pipeline

- Status: accepted for Phase 2 Slice 3
- Scope: local, read-only research artifacts; no broker or live execution

## Decision

The formal research pipeline uses filesystem adapters over immutable, content-addressed artifacts.
Artifact identifiers are exactly 64 lowercase hexadecimal characters. Readers constrain resolved
paths to their configured namespace, reject symlinks, validate schema, manifest identity, data
checksum and exact lineage, and never resolve `latest` or `current`. Records are exposed as typed
application objects in bounded batches; domain objects do not depend on JSONL, Parquet, DuckDB or
FinMind response types.

The pipeline order is pinned normalized data, PIT universe, features, labels, expanding baseline
model, selected OOS predictions, targets, event-driven backtest, analytics and immutable artifacts.
Every completed stage is atomically published before its checkpoint. Resume validates the exact
artifact before allowing a downstream stage to use it. Equal inputs, config and seed produce equal
content identities; creation time and filesystem paths are excluded. Resource policy bounds
instruments, calendar range and rows, and requires `--confirm-large-run` above those limits.

The selected-prediction adapter rejects training/validation rows, unaudited holdout access,
overlapping ownership, mixed lineage, unavailable predictions, unsafe fit cutoffs and observations
outside PIT eligibility or listing boundaries.

## Real-cache exploratory boundary

The first local run reads only existing, legally obtained files under `data/cache`; it does not
delete, refresh or blend them with synthetic fixtures. It uses multiple instruments and a bounded
period, then executes the existing long-only TWD event-driven accounting engine. Its fixed label is:

`EXPLORATORY — INCOMPLETE POINT-IN-TIME REFERENCE DATA`

Coverage explicitly records provisional listing dates and missing delisted history, corporate
actions, suspension history, price-limit tradability, licensed benchmark and market cap. These are
promotion blockers. The expanding baseline and backtest are engineering integration evidence, not
alpha, investment advice or validated performance.

## Dashboard and safety

Pipeline mode is mutually exclusive with demo and backtest-artifact modes. It is GET-only and shows
the exact run, stage status, output versions, coverage, failure reason and blockers in text. It does
not fall back to demo, expose paths, submit orders, connect a broker or enable live trading.
