# Production evidence intake runbook

This workflow is offline and no-go by construction. It does not select a provider, grant rights,
read credentials, connect Shioaji, implement an adapter/backend/control plane, deploy, or trade.

## Safety boundary

Prepare four JSON files outside runtime directories. Use exact absolute regular-file paths; no
`latest`, directory discovery, symlink, relative path, or implicit default is accepted. Never put a
credential, token, API key, certificate password, URL userinfo, full confidential contract, or
secret value in any input, URL, summary, output, log, or repository file. Evidence metadata records
only ID, accountable owner, official/contract URL, document version, retrieval/review/expiry dates,
short non-secret summary plus its computed SHA-256, and matching expected/verified document SHA-256.

For real candidates set every input classification to `production_candidate`; synthetic evidence
is then rejected. Public marketing is not a contract right. Use `UNKNOWN` for unanswered provider
fields and `unknown` for unanswered decisions. Do not invent numeric SLA/history, entitlement,
deployment, secret backend, host, process, or capital values.

## Create a no-go draft

```bash
island-quant create-production-readiness-draft \
  --provider-questionnaire /exact/provider-answers.json \
  --license-questionnaire /exact/license-answers.json \
  --operator-decisions /exact/operator-answers.json \
  --broker-evidence /exact/broker-official-evidence.json \
  --output /exact/mutable-production-draft.json \
  --as-of 2026-09-24T12:00:00+08:00
```

Exit `3` means a valid no-go draft was written; exit `2` means invalid input. Existing output is
refused. After human review only, repeat with `--force` to replace that exact mutable draft. The
command does not read app config or runtime state. It cannot create a live-ready artifact.

## Review and handoff

Verify the canonical checksum, all `UNKNOWN` fields, evidence expiry, and every blocker. A safe
handoff contains only the draft plus separately controlled source documents. Do not commit
confidential contract text. Phase 4C qualification requires a separate explicit transformation and
review after 100% critical PIT coverage, zero unexplained disagreement, zero stream faults, zero
silent fallback, a last-common watermark, licensed rollback, implemented/drilled security controls,
and authoritative broker closure evidence all exist.

The checked-in fixtures are **synthetic**, **offline**, **not provider behavior**, **not licensed
production data**, **not legal advice**, and **not live-ready**.

## Blocker routing matrix

| Blocker | Owner | Required evidence | No-go gate | Next safe step |
|---|---|---|---|---|
| Provider answers absent | Provider/data owner | Exact product/schema/entitlement, SLA/history, PIT/stream semantics | Provider/license | Request written technical answers |
| License answers absent | Provider/legal owner | Written use, storage, backup, derived, audit, termination/deletion rights | Provider/license | Review the executed terms; do not accept them here |
| Operator decisions absent | Operator/security owner | Owned deployment, recovery, clock, approval, caps and drill evidence | Security/deployment | Complete questionnaire without implementation |
| Reconciliation absent | Data operations owner | 100% coverage, zero disagreement/fault/fallback, watermark, licensed rollback | Parallel reconciliation | Wait for an approved licensed candidate |
| Eight broker unknowns | Broker integration owner | Official versioned guarantees | Broker preservation | Request official clarification without login |
