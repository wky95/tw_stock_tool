# Security and deployment design boundary

This document defines decisions that must exist before implementation. It does not implement a
secret backend, control plane, deployment, live toggle or broker connection.

## Secret resolution

Domain objects retain only a provider, reference name and version name. A composition-root
`SecretResolver` may return an opaque, expiring handle and must prevent serialization, logging and
inheritance by unrelated child processes. Rotation evidence identifies the reference/version,
timestamp, verifier and whether the prior version was revoked; it never contains the value.

For a single local macOS operator, Keychain offers OS-managed encrypted items and per-item access
control. A managed secret manager is favored when a supervised host needs centralized policy,
short-lived identity, audit, rotation and revocation. Selection depends on deployment topology and
operator ownership; neither backend is implemented or preselected. Apple documentation confirms
Keychain storage and access-control capabilities but does not decide Island Quant operations.

Official Apple sources reviewed 2026-09-24:

- <https://support.apple.com/guide/security/keychain-data-protection-secb0694df1a/web>
- <https://support.apple.com/guide/keychain-access/welcome/mac>

## Process and network boundary

Use a dedicated non-admin process identity. State, logs and backup staging are owner-only; release
artifacts are immutable to the runtime identity. Public inbound access is forbidden. Outbound access
is deny-by-default with separately approved DNS names/ports for broker, data, time and monitoring.
Any allowlist change is audited and requires restart/reconciliation.

## Control-plane threat model

The future control plane is separate from the read-only Dashboard. It requires strong
authentication, role authorization, CSRF and replay resistance, short-lived sessions, rate limiting,
independent two-person approval and immutable audit. Threats include stolen operator sessions,
confused deputy actions, replay, CSRF, forged approvals, privilege escalation, audit deletion and a
compromised strategy process. No write route is safe merely because it listens on localhost.

## Audit, recovery and time

Operator/legal must set audit retention. Backups must be encrypted, access audited and restored in
an isolated drill against checksums before broker reconciliation. RPO and RTO are explicit durations,
not aspirations. A monotonic clock supports local intervals; UTC/time synchronization is monitored,
and drift beyond an approved threshold blocks new risk.

## Two-step and staged capital

Stages are disabled, read-only, credentialed simulation, no-submit shadow and staged capital.
Advancement never happens automatically. A request and independent approval carry identities,
roles, timestamps, evidence and expiry. Staged capital requires hard maximum capital/order notional,
loss rollback, instrument and order-count caps plus an owner. This schema is not a live toggle and
cannot activate anything in the current application.

Open decisions: deployment host and identities; Keychain versus managed manager; rotation and
revocation owner; control-plane identity provider and roles; outbound destinations; audit retention;
RPO/RTO; clock threshold; backup region; staged limits and rollback owner.

Phase 4C validates these as explicit, owned, expiring decisions in an offline pack. The validator
also requires owner-only permissions, a non-empty host/port allowlist, no public inbound, complete
control-plane threats, encrypted-backup restore evidence, numeric clock drift, independent approval
and staged-capital/rollback owners. See the
[security/deployment decision checklist](SECURITY_DEPLOYMENT_DECISION_CHECKLIST.md). Passing a schema
does not establish that any backend or deployment control exists.
