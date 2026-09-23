# Security and deployment decision checklist

This checklist validates decisions and evidence only. No secret backend, deployment, authenticated
control plane, live toggle or control endpoint is implemented.

- Operator selects a secret backend; references contain names only, never values.
- Rotation and revocation have an owner, expiry and non-secret drill evidence.
- A dedicated non-admin process identity owns state with owner-only permissions.
- Release files are immutable to the runtime identity and separated from mutable state.
- Outbound hostnames and ports are explicit; public inbound is forbidden.
- A future control plane is separated from Dashboard and covers authentication, authorization,
  CSRF, replay, short sessions, rate limits, immutable audit and the full threat list.
- Audit retention, encrypted backups, isolated checksum-verified restore drill, RPO and RTO are
  numeric and approved.
- Clock source, numeric drift threshold and observation are recorded; excess drift blocks risk.
- Requester and approver are different people and approval has an expiry.
- Maximum staged capital, order notional and loss rollback caps are positive and explicit.
- Manual kill-switch and rollback owners are named.

Any missing, unknown, expired or contradictory critical item is no-go. Completing this checklist is
not evidence that a control has been implemented or tested.
