"""Strict, deterministic, offline production-admission evaluator.

The evaluator reads one explicitly named JSON pack. It performs no discovery,
network access, credential resolution, runtime mutation, or provider selection.
Synthetic packs can prove only that these gates work; they can never establish
production or live-trading readiness.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

SCHEMA_VERSION: Literal[1] = 1
REQUIRED_LICENSE_DECISIONS = frozenset(
    {
        "non_display_algorithmic_trading",
        "storage",
        "retention",
        "backup_restore",
        "derived_use",
        "audit",
        "display_export_redistribution",
        "termination_export_deletion_certification",
        "target_host_process",
        "incident_recovery_sla",
    }
)
REQUIRED_BROKER_BLOCKERS = frozenset(
    {
        "stable_identity_uniqueness_scope",
        "lost_response_authoritative_negative_lookup",
        "reconnect_order_deal_baseline",
        "cancel_replace_fill_race",
        "event_retention_query_horizon",
        "session_token_expiry",
        "error_status_mapping",
        "version_compatibility_deprecation",
    }
)
REQUIRED_THREAT_CONTROLS = frozenset(
    {
        "authentication",
        "authorization",
        "csrf_protection",
        "replay_protection",
        "short_lived_sessions",
        "rate_limiting",
        "immutable_audit",
        "dashboard_separation",
        "stolen_session",
        "confused_deputy",
        "forged_approval",
        "privilege_escalation",
        "audit_deletion",
        "compromised_strategy_process",
    }
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DecisionStatus(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class DecisionOwner(StrictModel):
    owner_id: str
    name: str
    role: str

    @field_validator("owner_id", "name", "role")
    @classmethod
    def present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("owner fields must not be empty")
        return value


class EvidenceReference(StrictModel):
    evidence_id: str
    source_kind: Literal["official", "contract", "synthetic"]
    source_url: str
    document_version: str
    retrieved_on: date
    reviewed_on: date
    expires_on: date
    owner_id: str
    content: str
    sha256: str

    @field_validator("evidence_id", "document_version", "owner_id", "content")
    @classmethod
    def present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence fields must not be empty")
        return value

    @field_validator("source_url")
    @classmethod
    def absolute_https_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("evidence source_url must be an absolute credential-free HTTPS URL")
        if ".." in parsed.path.split("/"):
            raise ValueError("evidence source_url must not contain path traversal")
        return value

    @field_validator("sha256")
    @classmethod
    def sha256_digest(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        return value


class ProductionReadinessDecision(StrictModel):
    decision_id: str
    status: DecisionStatus
    owner_id: str
    reviewed_on: date
    expires_on: date
    evidence_ids: tuple[str, ...]
    question: str
    next_safe_step: str


class DatasetAdmission(StrictModel):
    dataset: str
    required_fields: tuple[str, ...]
    schema_version: str
    entitlement_version: str
    config_version: str
    record_coverage: Annotated[Decimal, Field(ge=0, le=1)]
    field_coverage: Annotated[Decimal, Field(ge=0, le=1)]
    pit_timestamp_present: bool
    history_start: date
    history_depth_days: Annotated[int, Field(gt=0)]
    latency_sla_milliseconds: Annotated[int, Field(gt=0)]
    evidence_ids: tuple[str, ...]

    @field_validator("required_fields")
    @classmethod
    def required_fields_present(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not field.strip() for field in value):
            raise ValueError("required_fields must contain named fields")
        return value


class ProviderTechnicalCapabilities(StrictModel):
    pit_announcement_revision: bool
    stable_identity_identifier_changes: bool
    listing_delisting_boundaries: bool
    suspension_resumption: bool
    price_limit_exceptions: bool
    unexpected_closure: bool
    corporate_action_corrections: bool
    benchmark_constituents_shares_float_market_cap: bool
    streaming_sequence_freshness_gap_recovery: bool
    correction_deletion_notices: bool


class ProviderAdmission(StrictModel):
    provider_name: str
    product_name: str
    product_version: str
    decision_status: DecisionStatus
    entitlement_expires_on: date
    owner_id: str
    evidence_ids: tuple[str, ...]
    technical_capabilities: ProviderTechnicalCapabilities
    datasets: tuple[DatasetAdmission, ...]
    license_decisions: dict[str, ProductionReadinessDecision]

    @field_validator("license_decisions")
    @classmethod
    def known_license_decisions(
        cls, value: dict[str, ProductionReadinessDecision]
    ) -> dict[str, ProductionReadinessDecision]:
        unknown = set(value) - REQUIRED_LICENSE_DECISIONS
        if unknown:
            raise ValueError(f"unknown license decision fields: {sorted(unknown)}")
        return value


class StreamChecks(StrictModel):
    stale: int
    gaps: int
    duplicates: int
    out_of_order: int
    correction_deletion_replay_passed: bool

    @field_validator("stale", "gaps", "duplicates", "out_of_order")
    @classmethod
    def nonnegative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("stream counters must not be negative")
        return value


class ReconciliationAdmission(StrictModel):
    candidate_dataset_version: str
    prior_dataset_version: str
    config_version: str
    schema_version: str
    entitlement_version: str
    candidate_namespace: str
    prior_namespace: str
    stable_identity_comparison: bool
    field_record_complete: bool
    announcement_revision_lag_accepted: bool
    identifier_boundaries_accepted: bool
    corporate_action_corrections_accepted: bool
    calendar_tradability_accepted: bool
    benchmark_market_cap_accepted: bool
    unexplained_critical_disagreements: int
    silent_fallbacks: int
    last_common_watermark: str
    cutover_approved: bool
    rollback_entitled: bool
    rollback_fresh: bool
    rollback_licensed: bool
    stop_if_rollback_unavailable: bool
    stream: StreamChecks
    owner_id: str
    evidence_ids: tuple[str, ...]


class SecurityDeploymentAdmission(StrictModel):
    secret_backend_decision: ProductionReadinessDecision
    selected_secret_backend: str
    rotation_revocation_decision: ProductionReadinessDecision
    process_identity: str
    filesystem_mode: int
    immutable_release_runtime_separation: bool
    outbound_allowlist: tuple[str, ...]
    public_inbound_allowed: bool
    threat_model_controls: frozenset[str]
    audit_retention_days: Annotated[int, Field(gt=0)]
    backup_encrypted: bool
    restore_drill_evidence_ids: tuple[str, ...]
    rpo_seconds: Annotated[int, Field(gt=0)]
    rto_seconds: Annotated[int, Field(gt=0)]
    clock_source: str
    maximum_clock_drift_milliseconds: Annotated[int, Field(gt=0)]
    observed_clock_drift_milliseconds: Annotated[int, Field(ge=0)]
    requested_by: str
    approved_by: str
    approval_expires_at: datetime
    maximum_capital: Annotated[Decimal, Field(gt=0)]
    maximum_order_notional: Annotated[Decimal, Field(gt=0)]
    loss_rollback_cap: Annotated[Decimal, Field(gt=0)]
    rollback_owner_id: str
    kill_switch_owner_id: str
    evidence_ids: tuple[str, ...]

    @field_validator("outbound_allowlist")
    @classmethod
    def host_port_allowlist(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for entry in value:
            host, separator, port = entry.rpartition(":")
            if (
                not separator
                or not host.strip()
                or not port.isdigit()
                or not 1 <= int(port) <= 65535
            ):
                raise ValueError("outbound allowlist entries must be hostname:port")
        return value


class BrokerBlocker(StrictModel):
    blocker_id: str
    status: Literal["blocked", "partial"]
    owner_id: str
    evidence_ids: tuple[str, ...]
    remaining_unknown: str
    required_evidence: str
    next_safe_step: str


class ReadinessPack(StrictModel):
    schema_version: Literal[1]
    pack_id: str
    scope: Literal["synthetic_offline", "production_candidate"]
    synthetic: bool
    offline: Literal[True]
    as_of: datetime
    owners: tuple[DecisionOwner, ...]
    evidence: tuple[EvidenceReference, ...]
    provider: ProviderAdmission
    reconciliation: ReconciliationAdmission
    security_deployment: SecurityDeploymentAdmission
    broker_blockers: tuple[BrokerBlocker, ...]


class AdmissionGate(StrictModel):
    gate_id: str
    passed: bool
    blockers: tuple[str, ...]


class AdmissionBlocker(StrictModel):
    code: str
    owner: str
    required_evidence: str
    next_safe_step: str


class AdmissionReport(StrictModel):
    schema_version: Literal[1]
    pack_id: str
    scope: str
    evaluated_as_of: datetime
    evaluation_passed: bool
    admission_status: Literal["admitted", "no_go"]
    production_admission: Literal["no_go"]
    live_trading_ready: Literal[False]
    synthetic_offline_only: bool
    gates: tuple[AdmissionGate, ...]
    blockers: tuple[AdmissionBlocker, ...]
    report_checksum: str

    def canonical_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _add(
    blockers: list[AdmissionBlocker],
    code: str,
    owner: str,
    required_evidence: str,
    next_safe_step: str,
) -> None:
    blockers.append(
        AdmissionBlocker(
            code=code,
            owner=owner or "UNASSIGNED",
            required_evidence=required_evidence,
            next_safe_step=next_safe_step,
        )
    )


def _decision_blockers(
    decision: ProductionReadinessDecision,
    *,
    as_of: date,
    known_owners: set[str],
    known_evidence: set[str],
) -> list[AdmissionBlocker]:
    result: list[AdmissionBlocker] = []
    if decision.owner_id not in known_owners:
        _add(
            result,
            f"missing_owner:{decision.decision_id}",
            decision.owner_id,
            "named accountable owner",
            decision.next_safe_step,
        )
    if decision.status is not DecisionStatus.APPROVED:
        _add(
            result,
            f"decision_not_approved:{decision.decision_id}",
            decision.owner_id,
            decision.question,
            decision.next_safe_step,
        )
    if decision.expires_on < as_of:
        _add(
            result,
            f"decision_expired:{decision.decision_id}",
            decision.owner_id,
            "renewed decision evidence",
            decision.next_safe_step,
        )
    if decision.reviewed_on > as_of:
        _add(
            result,
            f"decision_review_in_future:{decision.decision_id}",
            decision.owner_id,
            "review dated no later than pack as_of",
            decision.next_safe_step,
        )
    if not decision.evidence_ids or not set(decision.evidence_ids) <= known_evidence:
        _add(
            result,
            f"missing_evidence:{decision.decision_id}",
            decision.owner_id,
            "all referenced official or contract evidence",
            decision.next_safe_step,
        )
    return result


def evaluate_readiness_pack(pack: ReadinessPack) -> AdmissionReport:
    """Evaluate an already parsed pack without I/O or mutable state."""

    gates: list[AdmissionGate] = []
    blockers: list[AdmissionBlocker] = []
    owner_ids = [owner.owner_id for owner in pack.owners]
    evidence_ids = [item.evidence_id for item in pack.evidence]
    known_owners = set(owner_ids)
    known_evidence = set(evidence_ids)
    as_of = pack.as_of.date()

    evidence_blockers: list[AdmissionBlocker] = []
    if (pack.scope == "synthetic_offline") != pack.synthetic:
        _add(
            evidence_blockers,
            "scope_classification_mismatch",
            "operator",
            "scope and synthetic classification that agree",
            "correct the classification without relabeling synthetic evidence",
        )
    if len(owner_ids) != len(known_owners):
        _add(
            evidence_blockers,
            "duplicate_owner",
            "operator",
            "unique owner IDs",
            "deduplicate owners",
        )
    if len(evidence_ids) != len(known_evidence):
        _add(
            evidence_blockers,
            "duplicate_evidence",
            "operator",
            "unique evidence IDs",
            "deduplicate evidence",
        )
    for item in pack.evidence:
        if item.owner_id not in known_owners:
            _add(
                evidence_blockers,
                f"missing_owner:{item.evidence_id}",
                item.owner_id,
                "named evidence owner",
                "assign and review the evidence",
            )
        if item.reviewed_on > as_of or item.retrieved_on > item.reviewed_on:
            _add(
                evidence_blockers,
                f"invalid_review_date:{item.evidence_id}",
                item.owner_id,
                "chronologically valid retrieval and review dates",
                "repeat evidence review",
            )
        if item.expires_on < as_of:
            _add(
                evidence_blockers,
                f"expired_evidence:{item.evidence_id}",
                item.owner_id,
                "unexpired evidence",
                "retrieve and review current evidence",
            )
        if hashlib.sha256(item.content.encode()).hexdigest() != item.sha256:
            _add(
                evidence_blockers,
                f"checksum_mismatch:{item.evidence_id}",
                item.owner_id,
                "content matching the recorded SHA-256",
                "restore or re-review evidence",
            )
        if pack.scope == "production_candidate" and item.source_kind == "synthetic":
            _add(
                evidence_blockers,
                f"non_authoritative_source:{item.evidence_id}",
                item.owner_id,
                "official or contract evidence",
                "obtain authoritative written evidence",
            )
    blockers.extend(evidence_blockers)
    gates.append(
        AdmissionGate(
            gate_id="evidence",
            passed=not evidence_blockers,
            blockers=tuple(item.code for item in evidence_blockers),
        )
    )

    provider_blockers: list[AdmissionBlocker] = []
    provider = pack.provider
    if provider.owner_id not in known_owners:
        _add(
            provider_blockers,
            "missing_owner:provider",
            provider.owner_id,
            "named provider decision owner",
            "assign an accountable owner",
        )
    if provider.decision_status is not DecisionStatus.APPROVED:
        _add(
            provider_blockers,
            "provider_not_approved",
            provider.owner_id,
            "explicit provider/product/version admission",
            "obtain provider, legal, and operator review",
        )
    if provider.entitlement_expires_on < as_of:
        _add(
            provider_blockers,
            "entitlement_expired",
            provider.owner_id,
            "unexpired written entitlement",
            "stop use and renew or replace the entitlement",
        )
    if (
        not provider.provider_name.strip()
        or not provider.product_name.strip()
        or not provider.product_version.strip()
    ):
        _add(
            provider_blockers,
            "provider_identity_missing",
            provider.owner_id,
            "exact provider, product, and version",
            "identify the exact contracted product",
        )
    if not provider.evidence_ids or not set(provider.evidence_ids) <= known_evidence:
        _add(
            provider_blockers,
            "provider_evidence_missing",
            provider.owner_id,
            "official or contract provider evidence",
            "attach reviewed evidence",
        )
    for capability, supported in provider.technical_capabilities.model_dump().items():
        if not supported:
            _add(
                provider_blockers,
                f"provider_capability_unknown:{capability}",
                provider.owner_id,
                f"official or contract evidence for {capability}",
                "obtain provider evidence or keep the candidate no-go",
            )
    for dataset in provider.datasets:
        if dataset.record_coverage != Decimal("1") or dataset.field_coverage != Decimal("1"):
            _add(
                provider_blockers,
                f"coverage_below_threshold:{dataset.dataset}",
                provider.owner_id,
                "100% critical record and field coverage",
                "close every critical coverage gap",
            )
        if not dataset.pit_timestamp_present:
            _add(
                provider_blockers,
                f"pit_timestamp_missing:{dataset.dataset}",
                provider.owner_id,
                "announcement/revision availability timestamp",
                "supply and verify PIT timestamps",
            )
        if not dataset.evidence_ids or not set(dataset.evidence_ids) <= known_evidence:
            _add(
                provider_blockers,
                f"dataset_evidence_missing:{dataset.dataset}",
                provider.owner_id,
                "dataset capability and SLA evidence",
                "attach official or contract evidence",
            )
    missing_decisions = REQUIRED_LICENSE_DECISIONS - set(provider.license_decisions)
    for decision_id in sorted(missing_decisions):
        _add(
            provider_blockers,
            f"missing_license_decision:{decision_id}",
            provider.owner_id,
            "written provider/legal decision",
            "obtain the missing written answer",
        )
    for decision in provider.license_decisions.values():
        provider_blockers.extend(
            _decision_blockers(
                decision, as_of=as_of, known_owners=known_owners, known_evidence=known_evidence
            )
        )
    blockers.extend(provider_blockers)
    gates.append(
        AdmissionGate(
            gate_id="provider_and_license",
            passed=not provider_blockers,
            blockers=tuple(item.code for item in provider_blockers),
        )
    )

    reconciliation_blockers: list[AdmissionBlocker] = []
    recon = pack.reconciliation
    if recon.owner_id not in known_owners:
        _add(
            reconciliation_blockers,
            "missing_owner:reconciliation",
            recon.owner_id,
            "named reconciliation owner",
            "assign an accountable owner",
        )
    if recon.candidate_namespace == recon.prior_namespace:
        _add(
            reconciliation_blockers,
            "namespaces_not_separate",
            recon.owner_id,
            "separate immutable namespaces",
            "rerun in distinct namespaces",
        )
    boolean_checks = {
        "stable_identity_comparison_missing": recon.stable_identity_comparison,
        "field_record_incomplete": recon.field_record_complete,
        "revision_lag_not_accepted": recon.announcement_revision_lag_accepted,
        "identifier_boundaries_not_accepted": recon.identifier_boundaries_accepted,
        "corporate_action_corrections_not_accepted": recon.corporate_action_corrections_accepted,
        "calendar_tradability_not_accepted": recon.calendar_tradability_accepted,
        "benchmark_market_cap_not_accepted": recon.benchmark_market_cap_accepted,
        "cutover_not_approved": recon.cutover_approved,
        "rollback_not_entitled": recon.rollback_entitled,
        "rollback_not_fresh": recon.rollback_fresh,
        "rollback_not_licensed": recon.rollback_licensed,
        "unsafe_rollback_fallback": recon.stop_if_rollback_unavailable,
        "correction_deletion_replay_failed": recon.stream.correction_deletion_replay_passed,
    }
    for code, passed in boolean_checks.items():
        if not passed:
            _add(
                reconciliation_blockers,
                code,
                recon.owner_id,
                "accepted deterministic reconciliation evidence",
                "stop cutover and reconcile",
            )
    if recon.unexplained_critical_disagreements:
        _add(
            reconciliation_blockers,
            "provider_disagreement",
            recon.owner_id,
            "operator decision for every critical disagreement",
            "stop and obtain operator decision",
        )
    if recon.silent_fallbacks:
        _add(
            reconciliation_blockers,
            "silent_fallback",
            recon.owner_id,
            "zero silent fallbacks",
            "stop readers and select one exact version explicitly",
        )
    if not recon.last_common_watermark.strip():
        _add(
            reconciliation_blockers,
            "last_common_watermark_missing",
            recon.owner_id,
            "recorded last common watermark",
            "freeze versions and record the watermark",
        )
    for name, count in recon.stream.model_dump().items():
        if name != "correction_deletion_replay_passed" and count:
            _add(
                reconciliation_blockers,
                f"stream_{name}",
                recon.owner_id,
                f"zero unresolved stream {name}",
                "stop consumption and recover from the pinned watermark",
            )
    if not recon.evidence_ids or not set(recon.evidence_ids) <= known_evidence:
        _add(
            reconciliation_blockers,
            "reconciliation_evidence_missing",
            recon.owner_id,
            "checksum-pinned reconciliation evidence",
            "produce the deterministic report",
        )
    blockers.extend(reconciliation_blockers)
    gates.append(
        AdmissionGate(
            gate_id="parallel_reconciliation",
            passed=not reconciliation_blockers,
            blockers=tuple(item.code for item in reconciliation_blockers),
        )
    )

    security_blockers: list[AdmissionBlocker] = []
    security = pack.security_deployment
    security_blockers.extend(
        _decision_blockers(
            security.secret_backend_decision,
            as_of=as_of,
            known_owners=known_owners,
            known_evidence=known_evidence,
        )
    )
    if not security.selected_secret_backend.strip():
        _add(
            security_blockers,
            "secret_backend_not_selected",
            security.secret_backend_decision.owner_id,
            "explicit approved secret backend selection",
            "record the operator decision without implementing the backend",
        )
    security_blockers.extend(
        _decision_blockers(
            security.rotation_revocation_decision,
            as_of=as_of,
            known_owners=known_owners,
            known_evidence=known_evidence,
        )
    )
    if security.filesystem_mode & 0o077:
        _add(
            security_blockers,
            "unsafe_filesystem_permission",
            security.rollback_owner_id,
            "owner-only filesystem mode",
            "set and verify owner-only permissions",
        )
    if not security.process_identity.strip() or not security.immutable_release_runtime_separation:
        _add(
            security_blockers,
            "process_release_boundary_incomplete",
            security.rollback_owner_id,
            "dedicated identity and immutable release/runtime separation",
            "approve the process boundary",
        )
    if not security.outbound_allowlist:
        _add(
            security_blockers,
            "outbound_allowlist_empty",
            security.rollback_owner_id,
            "explicit hostname and port allowlist",
            "define and review required destinations",
        )
    if security.public_inbound_allowed:
        _add(
            security_blockers,
            "public_inbound_allowed",
            security.rollback_owner_id,
            "no public inbound",
            "remove public ingress",
        )
    if not security.threat_model_controls >= REQUIRED_THREAT_CONTROLS:
        _add(
            security_blockers,
            "control_plane_threat_model_incomplete",
            security.rollback_owner_id,
            "complete authenticated control-plane threat model",
            "complete independent security review",
        )
    if not security.backup_encrypted or not security.restore_drill_evidence_ids:
        _add(
            security_blockers,
            "backup_restore_evidence_missing",
            security.rollback_owner_id,
            "encrypted backup and successful restore drill evidence",
            "run an isolated restore drill",
        )
    if security.observed_clock_drift_milliseconds > security.maximum_clock_drift_milliseconds:
        _add(
            security_blockers,
            "clock_drift_threshold_exceeded",
            security.rollback_owner_id,
            "clock observation within the numeric gate",
            "block new risk and restore time sync",
        )
    if security.requested_by == security.approved_by:
        _add(
            security_blockers,
            "two_step_not_independent",
            security.rollback_owner_id,
            "independent requester and approver",
            "repeat approval with separate people",
        )
    for approval_role, owner_id in (
        ("requester", security.requested_by),
        ("approver", security.approved_by),
    ):
        if owner_id not in known_owners:
            _add(
                security_blockers,
                f"missing_{approval_role}_owner",
                owner_id,
                f"named two-step {approval_role}",
                "assign a known independent owner and repeat approval",
            )
    if security.approval_expires_at <= pack.as_of:
        _add(
            security_blockers,
            "approval_expired",
            security.rollback_owner_id,
            "unexpired approval",
            "repeat independent approval",
        )
    for owner_field, value in (
        ("rollback", security.rollback_owner_id),
        ("kill_switch", security.kill_switch_owner_id),
    ):
        if value not in known_owners:
            _add(
                security_blockers,
                f"missing_{owner_field}_owner",
                value,
                f"named {owner_field} owner",
                "assign and record an accountable owner",
            )
    if not security.evidence_ids or not set(security.evidence_ids) <= known_evidence:
        _add(
            security_blockers,
            "security_evidence_missing",
            security.rollback_owner_id,
            "deployment and approval evidence",
            "attach reviewed non-secret evidence",
        )
    blockers.extend(security_blockers)
    gates.append(
        AdmissionGate(
            gate_id="security_deployment",
            passed=not security_blockers,
            blockers=tuple(item.code for item in security_blockers),
        )
    )

    broker_gate_blockers: list[AdmissionBlocker] = []
    by_id = {item.blocker_id: item for item in pack.broker_blockers}
    for blocker_id in sorted(REQUIRED_BROKER_BLOCKERS):
        broker_item = by_id.get(blocker_id)
        if broker_item is None:
            _add(
                broker_gate_blockers,
                f"broker_blocker_missing:{blocker_id}",
                "broker_integration_owner",
                "official source evidence retaining the unresolved blocker",
                "repeat official-source review",
            )
            continue
        if broker_item.owner_id not in known_owners:
            _add(
                broker_gate_blockers,
                f"missing_owner:broker:{blocker_id}",
                broker_item.owner_id,
                "named broker semantic owner",
                broker_item.next_safe_step,
            )
        official = [
            e
            for e in pack.evidence
            if e.evidence_id in broker_item.evidence_ids and e.source_kind == "official"
        ]
        if not official:
            _add(
                broker_gate_blockers,
                f"broker_official_evidence_missing:{blocker_id}",
                broker_item.owner_id,
                broker_item.required_evidence,
                broker_item.next_safe_step,
            )
        if not broker_item.remaining_unknown.strip():
            _add(
                broker_gate_blockers,
                f"broker_unknown_erased:{blocker_id}",
                broker_item.owner_id,
                broker_item.required_evidence,
                broker_item.next_safe_step,
            )
    blockers.extend(broker_gate_blockers)
    gates.append(
        AdmissionGate(
            gate_id="broker_blocker_preservation",
            passed=not broker_gate_blockers,
            blockers=tuple(item.code for item in broker_gate_blockers),
        )
    )

    ordered_blockers = tuple(sorted(blockers, key=lambda item: (item.code, item.owner)))
    passed = not ordered_blockers
    admission_status: Literal["admitted", "no_go"] = "admitted" if passed else "no_go"
    draft = AdmissionReport(
        schema_version=SCHEMA_VERSION,
        pack_id=pack.pack_id,
        scope=pack.scope,
        evaluated_as_of=pack.as_of,
        evaluation_passed=passed,
        admission_status=admission_status,
        production_admission="no_go",
        live_trading_ready=False,
        synthetic_offline_only=pack.synthetic,
        gates=tuple(gates),
        blockers=ordered_blockers,
        report_checksum="",
    )
    payload = draft.model_dump(mode="json")
    payload.pop("report_checksum")
    checksum = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    return draft.model_copy(update={"report_checksum": checksum})


def load_and_evaluate_readiness_pack(path: Path) -> AdmissionReport:
    """Read exactly ``path`` and return a deterministic report."""

    if not path.is_absolute():
        raise ValueError("readiness pack path must be absolute")
    if path.is_symlink() or not path.is_file():
        raise ValueError("readiness pack must be an existing regular non-symlink file")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        pack = ReadinessPack.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid readiness pack: {exc}") from exc
    return evaluate_readiness_pack(pack)


def report_json(report: AdmissionReport) -> str:
    """Canonical, stable JSON representation used by the CLI and fixtures."""

    return json.dumps(
        report.canonical_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
