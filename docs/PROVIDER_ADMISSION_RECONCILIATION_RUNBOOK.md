# Provider admission and reconciliation runbook

No provider is selected or recommended. All examples are synthetic, offline, not provider behavior,
not licensed production data and not live-ready.

## Candidate evidence

Legal, provider and operator owners must identify the exact product/version and supply official or
contract evidence with review and expiry dates. Complete every technical and rights field in
`readiness/provider_evaluation_template.json` and `readiness/license_questionnaire.json`. Marketing
does not establish non-display, algorithmic-trading, storage, backup, derived-use, audit, export,
redistribution or deletion rights. Unknown remains `UNKNOWN` and is no-go.

## Parallel run

Pin dataset, schema, configuration and entitlement versions. Write candidate and prior inputs to
separate immutable namespaces. Compare by stable instrument identity across required fields,
listing/delisting, identifier changes, announcement/revision lag, corporate-action corrections,
calendar/tradability, benchmark constituents/shares/float/market cap, and correction/deletion
replay. Streaming must report freshness, gaps, duplicates, out-of-order observations and recovery.

Acceptance requires 100% critical record and field coverage, PIT timestamps, zero unexplained
critical disagreement, zero unresolved stream faults and zero silent fallback. A critical
disagreement needs a recorded operator decision; tooling does not select the winning value.

## Cutover and rollback

Freeze and checksum both versions, record the last common watermark, stop writers, verify current
entitlement and rights, switch one exact version, replay and reconcile before releasing readers.
Rollback is allowed only to the exact prior version while it remains licensed, entitled, fresh and
complete. Otherwise stop. Never fall back to an expired, stale, incomplete or unlicensed source.
