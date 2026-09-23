from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from island_quant.readiness.admission import (
    ReadinessPack,
    evaluate_readiness_pack,
    load_and_evaluate_readiness_pack,
    report_json,
)

PACK_PATH = Path("docs/readiness/phase4c_synthetic_readiness_pack.json").resolve()


def raw_pack() -> dict[str, Any]:
    return json.loads(PACK_PATH.read_text(encoding="utf-8"))


def evaluate(raw: dict[str, Any]):  # type: ignore[no-untyped-def]
    return evaluate_readiness_pack(ReadinessPack.model_validate(raw))


def codes(raw: dict[str, Any]) -> set[str]:
    return {item.code for item in evaluate(raw).blockers}


def test_valid_complete_synthetic_pack_passes_but_never_claims_live_readiness() -> None:
    report = load_and_evaluate_readiness_pack(PACK_PATH)
    assert report.evaluation_passed
    assert report.admission_status == "admitted"
    assert report.production_admission == "no_go"
    assert report.live_trading_ready is False
    assert report.synthetic_offline_only


def test_missing_and_unknown_critical_decisions_fail() -> None:
    missing = raw_pack()
    del missing["provider"]["license_decisions"]["retention"]
    assert "missing_license_decision:retention" in codes(missing)

    unknown = raw_pack()
    unknown["provider"]["license_decisions"]["backup_restore"]["status"] = "unknown"
    assert "decision_not_approved:backup_restore" in codes(unknown)


def test_incompatible_scope_classification_fails() -> None:
    raw = raw_pack()
    raw["scope"] = "production_candidate"
    assert "scope_classification_mismatch" in codes(raw)


def test_expired_evidence_and_entitlement_fail() -> None:
    evidence = raw_pack()
    evidence["evidence"][0]["expires_on"] = "2026-09-23"
    assert "expired_evidence:synthetic-provider-evidence" in codes(evidence)

    entitlement = raw_pack()
    entitlement["provider"]["entitlement_expires_on"] = "2026-09-23"
    assert "entitlement_expired" in codes(entitlement)


def test_missing_or_invalid_owner_fails() -> None:
    raw = raw_pack()
    raw["provider"]["owner_id"] = ""
    assert "missing_owner:provider" in codes(raw)


def test_unknown_field_and_unsupported_schema_version_fail_fast() -> None:
    extra = raw_pack()
    extra["latest"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ReadinessPack.model_validate(extra)

    version = raw_pack()
    version["schema_version"] = 2
    with pytest.raises(ValidationError, match="Input should be 1"):
        ReadinessPack.model_validate(version)

    nested = raw_pack()
    nested["provider"]["license_decisions"]["marketing_claim"] = copy.deepcopy(
        nested["provider"]["license_decisions"]["storage"]
    )
    with pytest.raises(ValidationError, match="unknown license decision"):
        ReadinessPack.model_validate(nested)


@pytest.mark.parametrize("url", ["evidence/doc.txt", "https://example.invalid/../secret"])
def test_relative_or_path_traversal_evidence_reference_fails(url: str) -> None:
    raw = raw_pack()
    raw["evidence"][0]["source_url"] = url
    with pytest.raises(ValidationError, match="source_url"):
        ReadinessPack.model_validate(raw)


def test_evidence_checksum_mismatch_fails() -> None:
    raw = raw_pack()
    raw["evidence"][0]["content"] = "tampered"
    assert "checksum_mismatch:synthetic-provider-evidence" in codes(raw)


def test_license_contradiction_and_retention_backup_deletion_unknown_fail() -> None:
    raw = raw_pack()
    raw["provider"]["license_decisions"]["non_display_algorithmic_trading"]["status"] = "rejected"
    raw["provider"]["license_decisions"]["retention"]["status"] = "unknown"
    raw["provider"]["license_decisions"]["backup_restore"]["status"] = "unknown"
    raw["provider"]["license_decisions"]["termination_export_deletion_certification"]["status"] = (
        "unknown"
    )
    result = codes(raw)
    assert "decision_not_approved:non_display_algorithmic_trading" in result
    assert "decision_not_approved:retention" in result
    assert "decision_not_approved:backup_restore" in result
    assert "decision_not_approved:termination_export_deletion_certification" in result


def test_coverage_below_threshold_and_missing_pit_timestamp_fail() -> None:
    raw = raw_pack()
    raw["provider"]["datasets"][0]["record_coverage"] = "0.999999"
    raw["provider"]["datasets"][0]["pit_timestamp_present"] = False
    result = codes(raw)
    assert "coverage_below_threshold:synthetic-critical-pit-bundle" in result
    assert "pit_timestamp_missing:synthetic-critical-pit-bundle" in result


def test_provider_disagreement_silent_fallback_and_missing_watermark_fail() -> None:
    raw = raw_pack()
    raw["reconciliation"]["unexplained_critical_disagreements"] = 1
    raw["reconciliation"]["silent_fallbacks"] = 1
    raw["reconciliation"]["last_common_watermark"] = ""
    result = codes(raw)
    assert {"provider_disagreement", "silent_fallback", "last_common_watermark_missing"} <= result


def test_unlicensed_rollback_source_fails() -> None:
    raw = raw_pack()
    raw["reconciliation"]["rollback_licensed"] = False
    assert "rollback_not_licensed" in codes(raw)


@pytest.mark.parametrize("fault", ["stale", "gaps", "duplicates", "out_of_order"])
def test_stream_faults_fail(fault: str) -> None:
    raw = raw_pack()
    raw["reconciliation"]["stream"][fault] = 1
    assert f"stream_{fault}" in codes(raw)


def test_missing_rotation_evidence_and_unsafe_deployment_fail() -> None:
    raw = raw_pack()
    raw["security_deployment"]["rotation_revocation_decision"]["status"] = "unknown"
    raw["security_deployment"]["filesystem_mode"] = 420
    raw["security_deployment"]["outbound_allowlist"] = []
    raw["security_deployment"]["public_inbound_allowed"] = True
    raw["security_deployment"]["threat_model_controls"].remove("replay_protection")
    result = codes(raw)
    assert "decision_not_approved:rotation_revocation" in result
    assert "unsafe_filesystem_permission" in result
    assert "outbound_allowlist_empty" in result
    assert "public_inbound_allowed" in result
    assert "control_plane_threat_model_incomplete" in result


def test_clock_two_step_approval_and_rollback_owners_fail() -> None:
    raw = raw_pack()
    security = raw["security_deployment"]
    security["observed_clock_drift_milliseconds"] = 1001
    security["approved_by"] = security["requested_by"]
    security["approval_expires_at"] = "2026-09-24T11:59:59+08:00"
    security["rollback_owner_id"] = "missing-owner"
    security["kill_switch_owner_id"] = "missing-owner"
    result = codes(raw)
    assert "clock_drift_threshold_exceeded" in result
    assert "two_step_not_independent" in result
    assert "approval_expired" in result
    assert "missing_rollback_owner" in result
    assert "missing_kill_switch_owner" in result


def test_missing_staged_capital_cap_is_schema_failure() -> None:
    raw = raw_pack()
    del raw["security_deployment"]["maximum_capital"]
    with pytest.raises(ValidationError, match="maximum_capital"):
        ReadinessPack.model_validate(raw)


def test_broker_blocker_cannot_be_marked_resolved_without_official_evidence() -> None:
    resolved = raw_pack()
    resolved["broker_blockers"][0]["status"] = "resolved"
    with pytest.raises(ValidationError, match="blocked"):
        ReadinessPack.model_validate(resolved)

    synthetic = raw_pack()
    synthetic["broker_blockers"][0]["evidence_ids"] = ["synthetic-provider-evidence"]
    assert "broker_official_evidence_missing:stable_identity_uniqueness_scope" in codes(synthetic)


def test_report_and_checksum_are_deterministic() -> None:
    first = load_and_evaluate_readiness_pack(PACK_PATH)
    second = load_and_evaluate_readiness_pack(PACK_PATH)
    assert report_json(first) == report_json(second)
    payload = first.model_dump(mode="json")
    checksum = payload.pop("report_checksum")
    expected = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert checksum == expected


def test_cli_is_read_only_and_does_not_mutate_runtime_state(tmp_path: Path) -> None:
    target = tmp_path / "pack.json"
    target.write_text(json.dumps(raw_pack()), encoding="utf-8")
    before = copy.deepcopy(sorted(path.name for path in tmp_path.iterdir()))
    before_checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    result = subprocess.run(
        [".venv/bin/island-quant", "validate-production-readiness", "--pack", str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["evaluation_passed"] is True
    assert sorted(path.name for path in tmp_path.iterdir()) == before
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before_checksum


def test_cli_rejects_relative_pack_path() -> None:
    result = subprocess.run(
        [
            ".venv/bin/island-quant",
            "validate-production-readiness",
            "--pack",
            "docs/readiness/phase4c_synthetic_readiness_pack.json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "must be absolute" in result.stderr


def test_checked_in_no_go_report_has_deterministic_checksum_and_actionable_blockers() -> None:
    path = Path("docs/readiness/phase4c_production_readiness.json")
    report = json.loads(path.read_text(encoding="utf-8"))
    checksum = report.pop("report_checksum")
    expected = hashlib.sha256(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert checksum == expected
    assert report["admission_status"] == "no_go"
    assert report["live_trading_ready"] is False
    assert all(
        {"code", "owner", "required_evidence", "next_safe_step"} <= set(blocker)
        for blocker in report["blockers"]
    )
