# Production data integration runbook

This is a design and offline acceptance runbook. No provider is selected, no paid entitlement is
accepted, and no production adapter or network connection exists.

## Safety boundary

1. Keep research fixtures and every candidate provider in separate immutable namespaces.
2. Store only `EntitlementReference` names in configuration and artifacts; never store tokens,
   account details, license documents or credentials.
3. Require event, announcement, provider-availability and ingestion times. Ingestion time never
   substitutes for announcement time.
4. Do not use a source for risk or execution when entitlement, freshness, sequence, tradability,
   calendar, identifier mapping or PIT coverage is unknown.
5. Never silently fall back. A provider change requires a versioned operator decision and a new
   reconciliation baseline.

## Candidate admission

Populate `readiness/provider_evaluation_template.json` and `readiness/license_questionnaire.json`
using provider contract evidence. Evaluate every row in `production_data_requirements.json`.
Critical record and field coverage is 100%; unexplained disagreement, unknown tradability,
unresolved gap and silent fallback thresholds are zero. Numeric latency and history windows must be
set by the strategy/risk owner and guaranteed by the provider rather than inferred from fixtures.

Required evidence covers stable instrument identity and symbol changes; listing/delisting;
announcement and revision history; corporate actions; suspension/resumption; price-limit values and
exceptions; planned and emergency calendar events; PIT benchmark constituents/shares/float/market
cap; stream sequence, correction, deletion and recovery; and all applicable rights.

## Offline conformance

Run:

```bash
pytest -q tests/test_phase4b_production_data.py tests/test_phase4b_security_contracts.py
```

Fixtures are invented engineering scenarios. A pass proves only that the provider-neutral boundary
fails closed; it does not certify any actual provider. The suite must include late announcements,
same-day corrections, listing/delisting boundaries, identifier changes, suspension/resumption,
price-limit exceptions, unexpected closures, missing constituents, stale events, gap/duplicate/
out-of-order sequences, expired entitlement, excess retention, disagreement, silent fallback and
incomplete PIT coverage.

## Parallel run and reconciliation

1. Ingest the approved candidate into a new raw and normalized namespace without overwriting the
   current exploratory cache.
2. Pin provider, schema, entitlement, code and configuration versions plus checksums.
3. Compare the full acceptance window by stable instrument identity, not ticker alone.
4. Produce daily completeness, revision-lag, identifier, tradability, calendar, benchmark and
   numeric-value differences. Preserve both values; do not choose a winner automatically.
5. For streaming, record freshness, last accepted sequence, duplicates, gaps, out-of-order events,
   recovery attempts and correction/deletion notices.
6. Require zero unexplained critical differences through the operator-approved parallel window.
7. Promote by an explicit configuration version and approval evidence. There is no `latest` or
   implicit fallback.

## Migration and rollback

Migration freezes both dataset versions, records the last common watermark, stops writers, verifies
entitlement and coverage, switches one explicit provider version, and reconciles before readers are
released. Rollback stops new decisions, restores the previous still-entitled version, replays from
the common watermark and requires a fresh reconciliation. If the previous provider is expired,
stale or unlicensed, rollback means stopping; it does not mean serving bad data.

Immediate stop conditions include missing revisions, identifier ambiguity, unexpected calendar or
tradability state, stream staleness/gap, license uncertainty, retention breach, unexplained provider
disagreement, or incomplete PIT coverage.

## Retention, deletion and backups

For each dataset, legal/provider/operator must record raw, normalized, derived, audit and backup
retention; encryption and access rules; region; deletion deadline; backup expiry; termination export;
and whether derived artifacts may survive source deletion. A missing cell is `unknown` and blocks
production use. Deletion must produce non-secret evidence without erasing required audit lineage.
The parallel-run, migration, rollback and retention matrix is also machine-readable in
`readiness/production_data_transition_plan.json`.

## Operator handoff

Before implementation, obtain written answers for numeric latency/history, licensed environments and
processes, non-display/algorithmic use, storage/backups, derived use, audit retention, display/export,
incident SLA, termination/deletion and correction recovery. No candidate is recommended by this
runbook.

Phase 4C requires these answers in one strict evidence pack before any candidate can pass its
declared scope. Run the exact pack through `validate-production-readiness`; never use `latest`, file
discovery or an implicit fallback. Detailed cutover and rollback gates are in
[Provider admission and reconciliation](PROVIDER_ADMISSION_RECONCILIATION_RUNBOOK.md).
