# Production data readiness matrix and migration plan

Research date: **2026-09-24**. This plan does not purchase, license, or connect a data provider.
FinMind remains useful for exploratory work but is not accepted as the unqualified production source.

| Dataset/capability | Current coverage | Production requirement | Status |
|---|---|---|---|
| Licensed replacement provider | FinMind adapter and immutable cache | Contracted source with production/non-display, storage, backup, derived-use, audit, and incident rights | requires licensed data |
| PIT listing/delisting/instrument master | Versioned snapshots, incomplete historical references | Exchange-authoritative effective and announcement timestamps, identifiers, boards, lots, ticks, eligibility | partial |
| Corporate actions | Revision/availability model exists | Complete dividends, splits, capital changes, rights, effective/ex/announcement times and corrections | requires licensed data |
| Suspension/price-limit tradability | Missing authoritative production feed | Real-time and historical suspension/resumption, limits, exceptions, disposition and unexpected state | requires licensed data |
| Calendar/unexpected closure | Pinned fixture calendar | Official session calendar plus same-day delay/closure/emergency change channel | partial |
| Benchmark/market-cap history | Backtest contracts require pinned versions | Licensed PIT constituents, divisor/index values, shares, float and market cap with revision history | requires licensed data |
| Real-time/delayed market data | No production transport | Entitled streaming source, freshness/sequence/gap health, recovery, clock and target-host rights | blocked |
| Retention/audit/redistribution | Immutable local artifacts, no production license policy | Contract-specific retention, encryption, deletion, backups, access audit, display and redistribution rules | requires operator decision |

TWSE states that applicants for covered trading information must execute the appropriate agreement
and pay fees. Real-time redistribution has explicit licensing/fees; delayed data is defined as 20+
minutes and also requires an agreement. TWSE says value-added transmission to others requires
consent, while solely personal use may be treated differently. Those statements do not decide this
system's non-display, retention, backup, or derived-artifact rights; obtain written coverage.

Official sources retrieved 2026-09-24:

- <https://www.twse.com.tw/en/products/information/use.html>
- <https://www.twse.com.tw/en/products/information/real-time.html>
- <https://www.twse.com.tw/en/products/information/information.html>
- <https://www.twse.com.tw/en/products/information/qa.html>
- <https://eshop.twse.com.tw/en/>
- <https://openapi.twse.com.tw/>

## Provider contract

Every candidate provider must return source event time, provider publication/availability time,
ingestion time, revision identity, schema version, entitlement/license identifier, checksum, and
stable instrument identity. It must expose completeness watermarks, sequence/gap health, correction
and deletion notices, and documented recovery. Missing fields fail closed; ingestion time is never a
substitute for announcement time.

## Migration sequence

1. Inventory strategy/risk/accounting fields and write a field-level entitlement questionnaire.
2. Evaluate official exchange products and contracted vendors; obtain written non-display,
   algorithmic trading, retention, backup, derived-data and audit rights.
3. Build an offline fixture and raw immutable adapter under a new namespace; never overwrite
   FinMind history.
4. Produce parallel normalized snapshots and compare PIT coverage, timestamps, identifiers,
   corporate actions, calendar, price limits, suspensions, benchmark and market cap.
5. Require deterministic lineage and reconciliation reports across a representative historical
   window plus unexpected-event drills.
6. Promote the new provider only by explicit configuration version and operator approval. Retain a
   rollback path, but never silently fall back to an unlicensed or stale source in production.
