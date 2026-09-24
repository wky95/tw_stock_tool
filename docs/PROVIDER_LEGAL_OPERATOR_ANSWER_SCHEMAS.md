# Provider, legal, operator, and broker answer schemas

All Phase 4D models use schema version `1`, reject unknown fields, and require the same
`synthetic_offline` or `production_candidate` classification across the four inputs. Accountable
owners require non-empty ID, name, and role. References must resolve across the combined evidence
register.

## Evidence envelope

Each entry contains `evidence_id`, `owner_id`, `source_kind` (`official`, `contract`, or
`synthetic`), credential-free absolute HTTPS `source_url`, `document_version`, `retrieved_on`,
`reviewed_on`, `expires_on`, a short non-secret `summary`, computed `summary_sha256`,
`document_sha256`, and `verified_sha256`. Retrieval must not follow review, review must not follow
`--as-of`, the summary digest must match its text, and the two document digests must match. This
records verification without storing the source document.

## Provider answers

The exact provider/product/product-version, provider schema, entitlement identity, configuration,
required fields, positive numeric latency SLA, history start/depth, PIT announcement/revision,
stable identity, listing/delisting and identifier changes, suspension/resumption, price-limit
exceptions, unexpected closure, corporate-action corrections, benchmark constituents/shares/float/
market cap, streaming freshness/sequence/gap/recovery, corrections/deletions, incident/recovery SLA,
target host/process support, and critical record/field coverage are required keys. Unanswered values
are `UNKNOWN`, which remains no-go. Coverage below exactly `1.0` is no-go.

## Legal answers

The exact decision set covers non-display research/risk/trading, algorithmic trading, real-time and
historical rights, raw/normalized storage, retention, encrypted backup/restore, derived-use survival,
audit retention, display/export/redistribution, termination export, deletion deadline, backup
expiry, deletion certification, regions/subprocessors, and target hosts/processes. Status is only
`approved`, `rejected`, or `unknown`; approval without evidence is no-go.

## Operator answers

The exact decision set covers secret-backend selection, rotation/revocation owner, dedicated
identity, owner-only filesystem, immutable release/runtime separation, outbound hostname/port
allowlist, no public inbound, control-plane threat model, Dashboard separation, audit retention,
encrypted backup/restore drill, numeric RPO/RTO, clock source/drift gate, independent two-step
approval and expiry, staged capital/order/loss caps, and kill-switch/rollback ownership. The schema
records decisions only; it does not implement any control.

## Broker evidence

Exactly the eight preserved blocker IDs are required and status is only `blocked` or `partial`.
Every item records query date, official evidence ID, SDK/document version, summary, conclusion,
remaining unknown, required evidence, and next safe step. All referenced sources must be official;
an item cannot be removed or marked resolved.
