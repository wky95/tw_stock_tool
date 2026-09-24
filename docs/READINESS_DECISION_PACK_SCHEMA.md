# Readiness decision/evidence pack schema

Schema version 1 is implemented by `island_quant.readiness.admission`. Every object is strict:
unknown fields fail validation and no critical field has an implicit default.

## Top-level sections

- `pack_id`, `scope`, `synthetic`, `offline`, `as_of`: exact identity and evaluation boundary.
- `owners`: unique accountable owner IDs, names and roles.
- `evidence`: official, contract or synthetic provenance; absolute credential-free HTTPS source,
  document version, retrieval/review/expiry dates, owner, immutable content and SHA-256.
- `provider`: exact provider/product/version, entitlement expiry, dataset/config/schema/entitlement
  pins, numeric SLA, history start, PIT timestamp and 100% critical coverage.
- `license_decisions`: non-display/algorithmic trading, target process, storage, retention, backup,
  derived use, audit, display/export/redistribution, termination/export/deletion certification and
  incident/recovery SLA.
- `reconciliation`: immutable namespaces, stable identity comparison, all critical data boundaries,
  stream health, last-common watermark, explicit cutover and licensed/fresh rollback.
- `security_deployment`: secret-backend and rotation decisions, process/filesystem/network boundary,
  control-plane threat model, audit/backup/restore, RPO/RTO, clock gate, independent approval,
  staged-capital caps and kill/rollback owners.
- `broker_blockers`: all eight Phase 4B unknowns, still `blocked` or `partial`, with official evidence,
  remaining unknown, evidence needed and next safe step.

Decision status is exactly `approved`, `rejected` or `unknown`. Any critical value other than
`approved`, any missing/expired/unowned evidence, or an incompatible choice produces no-go.
Evidence content must match its lowercase SHA-256. References do not contain secrets, contract text
that cannot be committed, filesystem paths or credentials.

The deterministic report contains gate results, sorted blockers, scope, explicit production/live
no-go fields and a checksum over every field except the checksum itself.

Phase 4D uses a separate draft schema because this admission schema intentionally requires concrete
positive values and approved decisions. UNKNOWN must not be replaced with invented dates, SLAs,
limits, hosts, or rights. See `PROVIDER_LEGAL_OPERATOR_ANSWER_SCHEMAS.md`; a Phase 4D draft is never
accepted directly by this evaluator.
