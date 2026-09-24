"""Strict offline evidence intake and deterministic candidate comparison.

This module deliberately creates a no-go *draft*, not an admitted Phase 4C
pack.  It never discovers files, reads configuration, resolves credentials, or
touches runtime state.  UNKNOWN values remain explicit until an accountable
provider, legal reviewer, or operator supplies authoritative evidence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

UNKNOWN: Literal["UNKNOWN"] = "UNKNOWN"
INTAKE_SCHEMA_VERSION: Literal[1] = 1
DRAFT_SCHEMA_VERSION: Literal[1] = 1
COMPARISON_SCHEMA_VERSION: Literal[1] = 1

LICENSE_QUESTIONS = frozenset(
    {
        "non_display_research_risk_trading",
        "algorithmic_trading",
        "realtime_rights",
        "historical_rights",
        "raw_normalized_storage",
        "retention",
        "encrypted_backup_restore",
        "derived_use_survival",
        "audit_retention",
        "display_export_redistribution",
        "termination_export",
        "deletion_deadline",
        "backup_expiry",
        "deletion_certification",
        "regions_subprocessors",
        "target_hosts_processes",
    }
)
OPERATOR_QUESTIONS = frozenset(
    {
        "secret_backend_selection",
        "rotation_revocation_owner",
        "dedicated_process_identity",
        "owner_only_filesystem_mode",
        "immutable_release_runtime_separation",
        "outbound_hostname_port_allowlist",
        "no_public_inbound",
        "control_plane_threat_model",
        "dashboard_control_plane_separation",
        "audit_retention",
        "encrypted_backup_restore_drill",
        "rpo_rto",
        "clock_source_numeric_drift_gate",
        "two_step_independent_approval",
        "approval_expiry",
        "maximum_staged_capital",
        "maximum_order_notional",
        "loss_rollback_cap",
        "kill_switch_rollback_owner",
    }
)
BROKER_BLOCKERS = frozenset(
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


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IntakeOwner(StrictModel):
    owner_id: str
    name: str
    role: str

    @field_validator("owner_id", "name", "role")
    @classmethod
    def present(cls, value: str) -> str:
        if not value.strip() or value == UNKNOWN:
            raise ValueError("owner must be explicit and must not be UNKNOWN")
        return value


class IntakeEvidence(StrictModel):
    evidence_id: str
    owner_id: str
    source_kind: Literal["official", "contract", "synthetic"]
    source_url: str
    document_version: str
    retrieved_on: date
    reviewed_on: date
    expires_on: date
    summary: Annotated[str, Field(min_length=1, max_length=500)]
    summary_sha256: str
    document_sha256: str
    verified_sha256: str

    @field_validator("evidence_id", "owner_id", "document_version", "summary")
    @classmethod
    def present(cls, value: str) -> str:
        if not value.strip() or value == UNKNOWN:
            raise ValueError("evidence metadata must be explicit and must not be UNKNOWN")
        return value

    @field_validator("source_url")
    @classmethod
    def safe_official_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("evidence source must be an absolute credential-free HTTPS URL")
        if parsed.query or parsed.fragment:
            raise ValueError("evidence source must not contain query credentials or fragments")
        if ".." in parsed.path.split("/"):
            raise ValueError("evidence source must not contain path traversal")
        return value

    @field_validator("summary_sha256", "document_sha256", "verified_sha256")
    @classmethod
    def digest(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("evidence checksum must be a lowercase SHA-256 digest")
        return value


class ProviderAnswers(StrictModel):
    schema_version: Literal[1]
    classification: Literal["synthetic_offline", "production_candidate"]
    provider_name: str
    product_name: str
    product_version: str
    provider_schema_version: str
    entitlement_identity: str
    configuration_version: str
    owner: IntakeOwner
    evidence: tuple[IntakeEvidence, ...]
    required_fields: tuple[str, ...] | Literal["UNKNOWN"]
    numeric_latency_sla_milliseconds: Annotated[int, Field(gt=0)] | Literal["UNKNOWN"]
    history_start: date | Literal["UNKNOWN"]
    history_depth_days: Annotated[int, Field(gt=0)] | Literal["UNKNOWN"]
    pit_announcement_revision_semantics: str
    stable_instrument_identity: str
    listing_delisting_identifier_changes: str
    suspension_resumption: str
    price_limit_exceptions: str
    unexpected_closure: str
    corporate_action_corrections: str
    benchmark_constituents_shares_float_market_cap: str
    streaming_freshness_sequence_gap_recovery: str
    correction_deletion_notices: str
    incident_recovery_sla: str
    target_host_process_support: str
    record_coverage: Annotated[float, Field(ge=0, le=1)] | Literal["UNKNOWN"]
    field_coverage: Annotated[float, Field(ge=0, le=1)] | Literal["UNKNOWN"]
    evidence_ids: tuple[str, ...]

    @field_validator("provider_name", "product_name", "product_version")
    @classmethod
    def identity_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider/product/version must be present; use UNKNOWN if unanswered")
        return value


class DecisionAnswer(StrictModel):
    status: Literal["approved", "rejected", "unknown"]
    owner_id: str
    evidence_ids: tuple[str, ...]
    answer: Annotated[str, Field(min_length=1, max_length=1000)]


class LicenseAnswers(StrictModel):
    schema_version: Literal[1]
    classification: Literal["synthetic_offline", "production_candidate"]
    owner: IntakeOwner
    evidence: tuple[IntakeEvidence, ...]
    decisions: dict[str, DecisionAnswer]

    @field_validator("decisions")
    @classmethod
    def exact_questions(cls, value: dict[str, DecisionAnswer]) -> dict[str, DecisionAnswer]:
        if set(value) != LICENSE_QUESTIONS:
            raise ValueError("license decisions must contain exactly the documented questions")
        return value


class OperatorAnswers(StrictModel):
    schema_version: Literal[1]
    classification: Literal["synthetic_offline", "production_candidate"]
    owner: IntakeOwner
    evidence: tuple[IntakeEvidence, ...]
    decisions: dict[str, DecisionAnswer]

    @field_validator("decisions")
    @classmethod
    def exact_questions(cls, value: dict[str, DecisionAnswer]) -> dict[str, DecisionAnswer]:
        if set(value) != OPERATOR_QUESTIONS:
            raise ValueError("operator decisions must contain exactly the documented questions")
        return value


class BrokerEvidenceItem(StrictModel):
    blocker_id: str
    status: Literal["blocked", "partial"]
    owner_id: str
    evidence_ids: tuple[str, ...]
    query_date: date
    sdk_document_version: str
    evidence_summary: str
    conclusion: str
    remaining_unknown: str
    required_evidence: str
    next_safe_step: str

    @field_validator(
        "owner_id",
        "sdk_document_version",
        "evidence_summary",
        "conclusion",
        "remaining_unknown",
        "required_evidence",
        "next_safe_step",
    )
    @classmethod
    def present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("broker evidence fields must not be empty")
        return value


class BrokerEvidence(StrictModel):
    schema_version: Literal[1]
    classification: Literal["synthetic_offline", "production_candidate"]
    owner: IntakeOwner
    evidence: tuple[IntakeEvidence, ...]
    blockers: tuple[BrokerEvidenceItem, ...]


class DraftBlocker(StrictModel):
    code: str
    owner: str
    required_evidence: str
    next_safe_step: str


class DraftMetrics(StrictModel):
    critical_record_coverage: float | Literal["UNKNOWN"]
    critical_field_coverage: float | Literal["UNKNOWN"]
    unexplained_disagreements: int | Literal["UNKNOWN"]
    silent_fallbacks: int | Literal["UNKNOWN"]
    unresolved_stream_faults: int | Literal["UNKNOWN"]
    last_common_watermark: str
    rollback_licensed: Literal["approved", "rejected", "unknown"]
    entitlement_expires_on: date | Literal["UNKNOWN"]


class ProductionReadinessDraft(StrictModel):
    schema_version: Literal[1]
    pack_id: str
    scope: Literal["synthetic_offline", "production_candidate"]
    synthetic: bool
    offline: Literal[True]
    generated_at: datetime
    provider: ProviderAnswers
    license: LicenseAnswers
    operator: OperatorAnswers
    broker: BrokerEvidence
    metrics: DraftMetrics
    evidence: tuple[IntakeEvidence, ...]
    blockers: tuple[DraftBlocker, ...]
    evaluation_passed: Literal[False]
    admission_status: Literal["no_go"]
    production_admission: Literal["no_go"]
    live_trading_ready: Literal[False]
    report_checksum: str


class EvidenceChange(StrictModel):
    evidence_id: str
    change: Literal["added", "removed", "changed"]
    fields: tuple[str, ...]


class DecisionTransition(StrictModel):
    section: Literal["license", "operator"]
    decision_id: str
    previous: str
    candidate: str
    regression: bool


class ReadinessComparison(StrictModel):
    schema_version: Literal[1]
    previous_pack_id: str
    candidate_pack_id: str
    evidence_changes: tuple[EvidenceChange, ...]
    newly_expired: tuple[str, ...]
    expiring_within_30_days: tuple[str, ...]
    decision_transitions: tuple[DecisionTransition, ...]
    regressions: tuple[str, ...]
    admission_may_improve: bool
    admission_status: Literal["no_go"]
    live_trading_ready: Literal[False]
    report_checksum: str


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _checked_file(path: Path, *, role: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"{role} path must be absolute")
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{role} must be an existing regular non-symlink file")


def _load[ModelT: BaseModel](path: Path, model: type[ModelT], *, role: str) -> ModelT:
    _checked_file(path, role=role)
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise ValueError(f"invalid {role}: {exc}") from exc


def _validate_inputs(
    provider: ProviderAnswers,
    license_answers: LicenseAnswers,
    operator: OperatorAnswers,
    broker: BrokerEvidence,
    *,
    generated_at: datetime,
) -> tuple[Literal["synthetic_offline", "production_candidate"], tuple[IntakeEvidence, ...]]:
    classifications = {
        provider.classification,
        license_answers.classification,
        operator.classification,
        broker.classification,
    }
    if len(classifications) != 1:
        raise ValueError("all intake files must have the same classification")
    classification = classifications.pop()
    owners = {provider.owner.owner_id, license_answers.owner.owner_id, operator.owner.owner_id,
              broker.owner.owner_id}
    evidence = provider.evidence + license_answers.evidence + operator.evidence + broker.evidence
    evidence_ids = [item.evidence_id for item in evidence]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("evidence IDs must be unique across all intake files")
    known_evidence = set(evidence_ids)
    for evidence_item in evidence:
        if evidence_item.owner_id not in owners:
            raise ValueError(f"evidence {evidence_item.evidence_id} references an unknown owner")
        if (
            evidence_item.retrieved_on > evidence_item.reviewed_on
            or evidence_item.reviewed_on > generated_at.date()
        ):
            raise ValueError(f"evidence {evidence_item.evidence_id} has invalid review chronology")
        if evidence_item.document_sha256 != evidence_item.verified_sha256:
            raise ValueError(f"evidence {evidence_item.evidence_id} checksum verification failed")
        summary_checksum = hashlib.sha256(evidence_item.summary.encode()).hexdigest()
        if summary_checksum != evidence_item.summary_sha256:
            raise ValueError(f"evidence {evidence_item.evidence_id} summary checksum failed")
        if classification == "production_candidate" and evidence_item.source_kind == "synthetic":
            raise ValueError("synthetic evidence cannot enter a production_candidate draft")
    references: list[tuple[str, str, tuple[str, ...]]] = [
        ("provider", provider.owner.owner_id, provider.evidence_ids)
    ]
    references.extend(
        (f"license:{key}", decision.owner_id, decision.evidence_ids)
        for key, decision in license_answers.decisions.items()
    )
    references.extend(
        (f"operator:{key}", decision.owner_id, decision.evidence_ids)
        for key, decision in operator.decisions.items()
    )
    references.extend(
        (f"broker:{item.blocker_id}", item.owner_id, item.evidence_ids)
        for item in broker.blockers
    )
    for reference, owner_id, ids in references:
        if owner_id not in owners:
            raise ValueError(f"{reference} references an unknown owner")
        if not set(ids) <= known_evidence:
            raise ValueError(f"{reference} references unknown evidence")
    broker_ids = [item.blocker_id for item in broker.blockers]
    if set(broker_ids) != BROKER_BLOCKERS or len(broker_ids) != len(set(broker_ids)):
        raise ValueError("broker evidence must preserve exactly all eight semantic blockers")
    for broker_item in broker.blockers:
        official = [
            entry for entry in evidence if entry.evidence_id in broker_item.evidence_ids
        ]
        if not official or any(entry.source_kind != "official" for entry in official):
            raise ValueError(
                f"broker blocker {broker_item.blocker_id} requires official evidence only"
            )
    return classification, tuple(sorted(evidence, key=lambda item: item.evidence_id))


def _blockers(
    provider: ProviderAnswers,
    license_answers: LicenseAnswers,
    operator: OperatorAnswers,
    broker: BrokerEvidence,
    evidence: tuple[IntakeEvidence, ...],
    *,
    generated_at: datetime,
) -> tuple[DraftBlocker, ...]:
    result: list[DraftBlocker] = []

    def add(code: str, owner: str, required: str, step: str) -> None:
        result.append(DraftBlocker(code=code, owner=owner, required_evidence=required,
                                   next_safe_step=step))

    provider_values = provider.model_dump()
    provider_fields = (
        "provider_name", "product_name", "product_version", "provider_schema_version",
        "entitlement_identity", "configuration_version", "required_fields",
        "numeric_latency_sla_milliseconds", "history_start", "history_depth_days",
        "pit_announcement_revision_semantics", "stable_instrument_identity",
        "listing_delisting_identifier_changes", "suspension_resumption",
        "price_limit_exceptions", "unexpected_closure", "corporate_action_corrections",
        "benchmark_constituents_shares_float_market_cap",
        "streaming_freshness_sequence_gap_recovery", "correction_deletion_notices",
        "incident_recovery_sla", "target_host_process_support", "record_coverage",
        "field_coverage",
    )
    for field in provider_fields:
        value = provider_values[field]
        if value == UNKNOWN or value == ():
            add(f"provider_unknown:{field}", provider.owner.owner_id,
                f"authoritative provider answer for {field}", "obtain a written provider answer")
    if isinstance(provider.record_coverage, float) and provider.record_coverage < 1:
        add("coverage_below_100_percent:record", provider.owner.owner_id,
            "100% critical record coverage", "close every critical coverage gap")
    if isinstance(provider.field_coverage, float) and provider.field_coverage < 1:
        add("coverage_below_100_percent:field", provider.owner.owner_id,
            "100% critical field coverage", "close every critical coverage gap")
    if not provider.evidence_ids:
        add("provider_evidence_missing", provider.owner.owner_id,
            "official or contract provider evidence",
            "obtain and review authoritative provider documentation")
    for section, decisions in (("license", license_answers.decisions),
                               ("operator", operator.decisions)):
        for key, decision in decisions.items():
            if decision.status != "approved":
                add(f"{section}_decision_not_approved:{key}", decision.owner_id,
                    f"written {section} decision for {key}",
                    f"obtain accountable {section} review; do not infer approval")
            if decision.answer == UNKNOWN:
                add(
                    f"{section}_answer_unknown:{key}",
                    decision.owner_id,
                    f"substantive written {section} answer for {key}",
                    f"obtain accountable {section} review; do not infer an answer",
                )
            if decision.status == "approved" and not decision.evidence_ids:
                add(f"{section}_approved_without_evidence:{key}", decision.owner_id,
                    "official or contract evidence", "attach and review authoritative evidence")
    for evidence_item in evidence:
        if evidence_item.expires_on < generated_at.date():
            add(f"expired_evidence:{evidence_item.evidence_id}", evidence_item.owner_id,
                "unexpired evidence",
                "retrieve and review current evidence")
    for broker_item in broker.blockers:
        add(f"broker_blocked:{broker_item.blocker_id}", broker_item.owner_id,
            broker_item.required_evidence, broker_item.next_safe_step)
    for code, required in (
        (
            "reconciliation_unknown:unexplained_disagreements",
            "zero unexplained critical provider disagreements",
        ),
        ("reconciliation_unknown:silent_fallbacks", "zero silent fallbacks"),
        ("reconciliation_unknown:stream_faults", "zero unresolved stream faults"),
        (
            "reconciliation_unknown:last_common_watermark",
            "a pinned last-common watermark",
        ),
        (
            "reconciliation_unknown:rollback_license",
            "licensed, entitled, fresh rollback source",
        ),
        ("entitlement_expiry_unknown", "an explicit current entitlement expiry"),
    ):
        add(
            code,
            provider.owner.owner_id,
            required,
            "perform pinned reconciliation only after licensed provider approval",
        )
    add("phase4c_qualified_pack_not_created", operator.owner.owner_id,
        "qualified Phase 4C pack with real provider, legal, operator and reconciliation evidence",
        "keep this draft no-go until every critical answer and operational proof exists")
    return tuple(sorted(result, key=lambda item: (item.code, item.owner)))


def create_production_readiness_draft(
    *,
    provider_questionnaire: Path,
    license_questionnaire: Path,
    operator_decisions: Path,
    broker_evidence: Path,
    output: Path,
    generated_at: datetime,
    force: bool = False,
) -> ProductionReadinessDraft:
    """Create exactly one no-go draft at an explicit absolute output path."""

    if not output.is_absolute():
        raise ValueError("output path must be absolute")
    if output.is_symlink() or (output.exists() and not output.is_file()):
        raise ValueError("output must be a regular non-symlink file path")
    if output.exists() and not force:
        raise ValueError("output already exists; pass --force to replace this mutable draft")
    if not output.parent.is_dir():
        raise ValueError("output parent directory must already exist")
    provider = _load(provider_questionnaire, ProviderAnswers, role="provider questionnaire")
    license_answers = _load(license_questionnaire, LicenseAnswers, role="license questionnaire")
    operator = _load(operator_decisions, OperatorAnswers, role="operator decisions")
    broker = _load(broker_evidence, BrokerEvidence, role="broker evidence")
    now = generated_at
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    scope, evidence = _validate_inputs(provider, license_answers, operator, broker,
                                       generated_at=now)
    blockers = _blockers(provider, license_answers, operator, broker, evidence, generated_at=now)
    metrics = _draft_metrics(provider)
    pack_id = _draft_pack_id(provider, license_answers, operator, broker, scope)
    draft = ProductionReadinessDraft(
        schema_version=DRAFT_SCHEMA_VERSION,
        pack_id=pack_id,
        scope=scope,
        synthetic=scope == "synthetic_offline",
        offline=True,
        generated_at=now,
        provider=provider,
        license=license_answers,
        operator=operator,
        broker=broker,
        metrics=metrics,
        evidence=evidence,
        blockers=blockers,
        evaluation_passed=False,
        admission_status="no_go",
        production_admission="no_go",
        live_trading_ready=False,
        report_checksum="",
    )
    payload = draft.model_dump(mode="json")
    payload.pop("report_checksum")
    draft = draft.model_copy(update={"report_checksum": hashlib.sha256(
        _canonical_bytes(payload)).hexdigest()})
    output.write_text(json.dumps(draft.model_dump(mode="json"), ensure_ascii=False, indent=2,
                                 sort_keys=True) + "\n", encoding="utf-8")
    return draft


def _draft_metrics(provider: ProviderAnswers) -> DraftMetrics:
    return DraftMetrics(
        critical_record_coverage=provider.record_coverage,
        critical_field_coverage=provider.field_coverage,
        unexplained_disagreements=UNKNOWN,
        silent_fallbacks=UNKNOWN,
        unresolved_stream_faults=UNKNOWN,
        last_common_watermark=UNKNOWN,
        rollback_licensed="unknown",
        entitlement_expires_on=UNKNOWN,
    )


def _draft_pack_id(
    provider: ProviderAnswers,
    license_answers: LicenseAnswers,
    operator: OperatorAnswers,
    broker: BrokerEvidence,
    scope: str,
) -> str:
    seed = {
        "provider": provider.model_dump(mode="json"),
        "license": license_answers.model_dump(mode="json"),
        "operator": operator.model_dump(mode="json"),
        "broker": broker.model_dump(mode="json"),
    }
    return f"phase4d-{scope}-{hashlib.sha256(_canonical_bytes(seed)).hexdigest()[:16]}"


def load_draft(path: Path) -> ProductionReadinessDraft:
    draft = _load(path, ProductionReadinessDraft, role="readiness draft")
    payload = draft.model_dump(mode="json")
    checksum = payload.pop("report_checksum")
    expected = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    if checksum != expected:
        raise ValueError("readiness draft report_checksum does not match its content")
    scope, evidence = _validate_inputs(
        draft.provider,
        draft.license,
        draft.operator,
        draft.broker,
        generated_at=draft.generated_at,
    )
    expected_blockers = _blockers(
        draft.provider,
        draft.license,
        draft.operator,
        draft.broker,
        evidence,
        generated_at=draft.generated_at,
    )
    if scope != draft.scope or draft.synthetic != (scope == "synthetic_offline"):
        raise ValueError("readiness draft classification is inconsistent")
    if evidence != draft.evidence:
        raise ValueError("readiness draft combined evidence register is inconsistent")
    if expected_blockers != draft.blockers:
        raise ValueError("readiness draft blockers are inconsistent with its answers")
    if _draft_metrics(draft.provider) != draft.metrics:
        raise ValueError("readiness draft metrics are inconsistent with its answers")
    expected_pack_id = _draft_pack_id(
        draft.provider, draft.license, draft.operator, draft.broker, scope
    )
    if expected_pack_id != draft.pack_id:
        raise ValueError("readiness draft pack_id does not match its inputs")
    return draft


def _decision_map(
    draft: ProductionReadinessDraft,
) -> dict[tuple[Literal["license", "operator"], str], str]:
    result: dict[tuple[Literal["license", "operator"], str], str] = {}
    for key, decision in draft.license.decisions.items():
        result[("license", key)] = decision.status
    for key, decision in draft.operator.decisions.items():
        result[("operator", key)] = decision.status
    return result


def compare_production_readiness(
    previous: ProductionReadinessDraft, candidate: ProductionReadinessDraft
) -> ReadinessComparison:
    previous_evidence = {item.evidence_id: item for item in previous.evidence}
    candidate_evidence = {item.evidence_id: item for item in candidate.evidence}
    changes: list[EvidenceChange] = []
    regressions: list[str] = []
    for evidence_id in sorted(set(previous_evidence) | set(candidate_evidence)):
        left = previous_evidence.get(evidence_id)
        right = candidate_evidence.get(evidence_id)
        if left is None:
            changes.append(EvidenceChange(evidence_id=evidence_id, change="added", fields=()))
        elif right is None:
            changes.append(EvidenceChange(evidence_id=evidence_id, change="removed", fields=()))
            regressions.append(f"evidence_removed:{evidence_id}")
        elif left != right:
            left_values = left.model_dump(mode="json")
            right_values = right.model_dump(mode="json")
            fields = tuple(
                sorted(key for key in left_values if left_values[key] != right_values[key])
            )
            changes.append(EvidenceChange(evidence_id=evidence_id, change="changed", fields=fields))
            if {
                "document_sha256",
                "source_url",
                "document_version",
                "owner_id",
                "expires_on",
            } & set(fields):
                regressions.append(f"critical_evidence_changed:{evidence_id}")
    transitions: list[DecisionTransition] = []
    previous_decisions = _decision_map(previous)
    candidate_decisions = _decision_map(candidate)
    for key in sorted(previous_decisions):
        old = previous_decisions[key]
        new = candidate_decisions[key]
        if old != new:
            regression = old == "approved" and new != "approved"
            transitions.append(DecisionTransition(section=key[0], decision_id=key[1],
                                                  previous=old, candidate=new,
                                                  regression=regression))
            if regression:
                regressions.append(f"decision_regression:{key[0]}:{key[1]}:{old}_to_{new}")
    for metric in ("critical_record_coverage", "critical_field_coverage"):
        old = getattr(previous.metrics, metric)
        new = getattr(candidate.metrics, metric)
        if isinstance(old, float) and (new == UNKNOWN or (isinstance(new, float) and new < old)):
            regressions.append(f"coverage_regression:{metric}")
    if (
        previous.metrics.rollback_licensed == "approved"
        and candidate.metrics.rollback_licensed != "approved"
    ):
        regressions.append("reconciliation_regression:rollback_license")
    previous_broker = {item.blocker_id: item for item in previous.broker.blockers}
    candidate_broker = {item.blocker_id: item for item in candidate.broker.blockers}
    if set(previous_broker) - set(candidate_broker):
        regressions.append("broker_blocker_removed")
    previous_security = previous.operator.decisions
    candidate_security = candidate.operator.decisions
    if any(previous_security[key].status == "approved" and
           candidate_security[key].status != "approved" for key in previous_security):
        regressions.append("security_decision_regression")
    as_of = candidate.generated_at.date()
    old_expired = {
        item.evidence_id
        for item in previous.evidence
        if item.expires_on < previous.generated_at.date()
    }
    newly_expired = tuple(
        sorted(
            item.evidence_id
            for item in candidate.evidence
            if item.expires_on < as_of and item.evidence_id not in old_expired
        )
    )
    if newly_expired:
        regressions.extend(f"evidence_expired:{item}" for item in newly_expired)
    horizon = as_of + timedelta(days=30)
    expiring = tuple(sorted(item.evidence_id for item in candidate.evidence
                            if as_of <= item.expires_on <= horizon))
    regression_items = tuple(sorted(set(regressions)))
    positive_change = any(item.change == "added" for item in changes) or any(
        item.candidate == "approved" and item.previous != "approved" for item in transitions
    )
    report = ReadinessComparison(
        schema_version=COMPARISON_SCHEMA_VERSION,
        previous_pack_id=previous.pack_id,
        candidate_pack_id=candidate.pack_id,
        evidence_changes=tuple(changes),
        newly_expired=newly_expired,
        expiring_within_30_days=expiring,
        decision_transitions=tuple(transitions),
        regressions=regression_items,
        admission_may_improve=(
            positive_change
            and not regression_items
            and len(candidate.evidence) >= len(previous.evidence)
        ),
        admission_status="no_go",
        live_trading_ready=False,
        report_checksum="",
    )
    payload = report.model_dump(mode="json")
    payload.pop("report_checksum")
    return report.model_copy(update={"report_checksum": hashlib.sha256(
        _canonical_bytes(payload)).hexdigest()})


def load_and_compare(previous: Path, candidate: Path) -> ReadinessComparison:
    return compare_production_readiness(load_draft(previous), load_draft(candidate))


def canonical_json(value: BaseModel) -> str:
    return json.dumps(value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
