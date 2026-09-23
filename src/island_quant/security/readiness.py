"""Provider-neutral security/deployment policy contracts with no implementation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable


class SecretBackendKind(StrEnum):
    MACOS_KEYCHAIN = "macos_keychain"
    MANAGED_SECRET_MANAGER = "managed_secret_manager"


class EnablementStage(StrEnum):
    DISABLED = "disabled"
    READ_ONLY = "read_only"
    SIMULATION = "simulation"
    SHADOW = "shadow_no_submit"
    STAGED_CAPITAL = "staged_capital"


@dataclass(frozen=True, slots=True)
class SecretReference:
    provider: str
    reference_name: str
    version_name: str

    def __post_init__(self) -> None:
        values = (self.provider, self.reference_name, self.version_name)
        if not all(value.strip() for value in values):
            raise ValueError("secret references must be named")


@dataclass(frozen=True, slots=True)
class ResolvedSecret:
    """Opaque handle returned only to a composition root, never a serializable value."""

    reference: SecretReference
    opaque_handle: object
    expires_at: datetime


@runtime_checkable
class SecretResolver(Protocol):
    def resolve(self, reference: SecretReference) -> ResolvedSecret: ...

    def revoke(self, reference: SecretReference, *, reason: str) -> str: ...


@dataclass(frozen=True, slots=True)
class RotationEvidence:
    reference: SecretReference
    rotated_at: datetime
    previous_version_revoked: bool
    verified_by: str
    evidence_reference: str


@dataclass(frozen=True, slots=True)
class ProcessBoundaryPolicy:
    process_identity: str
    filesystem_owner: str
    state_mode: int
    allowed_outbound_hosts: frozenset[str]
    public_inbound_allowed: bool

    def __post_init__(self) -> None:
        if self.public_inbound_allowed:
            raise ValueError("production trading process cannot expose public inbound access")
        if self.state_mode & 0o077:
            raise ValueError("group/other filesystem permissions are forbidden")
        if not self.allowed_outbound_hosts:
            raise ValueError("an explicit outbound allowlist is required")


@dataclass(frozen=True, slots=True)
class ControlPlaneThreatModel:
    authentication_required: bool
    authorization_required: bool
    csrf_protection_required: bool
    replay_protection_required: bool
    immutable_audit_required: bool
    dashboard_separated: bool

    @property
    def complete(self) -> bool:
        return all(
            (
                self.authentication_required,
                self.authorization_required,
                self.csrf_protection_required,
                self.replay_protection_required,
                self.immutable_audit_required,
                self.dashboard_separated,
            )
        )


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    rpo: timedelta
    rto: timedelta
    audit_retention: timedelta
    encrypted_backups: bool
    restore_drill_required: bool

    def __post_init__(self) -> None:
        if min(self.rpo, self.rto, self.audit_retention) <= timedelta(0):
            raise ValueError("RPO, RTO, and audit retention must be positive")


@dataclass(frozen=True, slots=True)
class ClockDriftGate:
    maximum_drift: timedelta
    observed_drift: timedelta
    source: str

    @property
    def permits_new_risk(self) -> bool:
        return abs(self.observed_drift) <= self.maximum_drift


@dataclass(frozen=True, slots=True)
class ApprovalEvidence:
    approver: str
    role: str
    approved_at: datetime
    evidence_reference: str


@dataclass(frozen=True, slots=True)
class TwoStepEnablementPolicy:
    policy_version: str
    stage: EnablementStage
    requested_by: ApprovalEvidence
    approved_by: ApprovalEvidence
    expires_at: datetime
    maximum_capital: Decimal
    maximum_order_notional: Decimal
    rollback_loss: Decimal

    def __post_init__(self) -> None:
        if self.requested_by.approver == self.approved_by.approver:
            raise ValueError("two-step enablement requires independent people")
        if self.approved_by.approved_at < self.requested_by.approved_at:
            raise ValueError("approval cannot precede request")
        if self.expires_at <= self.approved_by.approved_at:
            raise ValueError("enablement approval must expire in the future")
        if min(self.maximum_capital, self.maximum_order_notional, self.rollback_loss) <= 0:
            raise ValueError("staged-capital and rollback limits must be positive")
