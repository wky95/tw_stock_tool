# ADR 0020: Production evidence intake and candidate qualification

## Status

Accepted for offline evidence intake only. This decision does not authorize procurement, contract
acceptance, credentials, broker/provider login, adapters, deployment, control-plane work, order
submission, live enablement, or staged capital.

## Decision

Phase 4D separates evidence intake from Phase 4C admission. Four explicitly named, absolute JSON
paths carry provider, legal, operator, and broker-official answers. Every schema is versioned and
strict; unknown fields, implicit discovery, symlinks, relative paths, URL userinfo, traversal,
unknown owners, dangling evidence references, checksum disagreement, invalid chronology, and
synthetic evidence in a `production_candidate` are rejected.

The output is a mutable, explicitly named `ProductionReadinessDraft`. It preserves `UNKNOWN`, all
eight Shioaji blockers, source/version/owner/dates/checksums, and deterministic blockers. A draft is
always `production_admission=no_go` and `live_trading_ready=false`; it is not a Phase 4C admitted
pack. This avoids inventing dates, numeric SLAs, capital limits, deployment choices, or contract
rights merely to satisfy the Phase 4C schema. Existing output is not overwritten without `--force`.

Comparison is canonical and checksum-addressed. It reports added, removed, and changed evidence;
source/version/checksum/owner/expiry changes; decision transitions; expiry; coverage, rollback,
broker, and security regressions. Removing evidence or weakening a critical decision cannot improve
admission. Both compared artifacts remain no-go.

Evidence metadata contains only a short non-secret summary and matching expected/verified SHA-256.
Contract text, credentials, tokens, certificate passwords, and secret values are never repository
inputs or outputs. Commands do not load application configuration or touch runtime databases,
caches, artifacts, or network services.

## Consequences

- Provider, legal, operator, and broker evidence can be reviewed without crossing a live boundary.
- Synthetic fixtures prove tooling behavior only and cannot become production candidates.
- Qualification into a Phase 4C pack remains a later explicit step after real answers,
  reconciliation, operational controls, and independent review exist.
- Current production readiness remains no-go.
