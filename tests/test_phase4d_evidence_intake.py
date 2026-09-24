from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from island_quant.readiness.intake import (
    ProductionReadinessDraft,
    canonical_json,
    compare_production_readiness,
    create_production_readiness_draft,
    load_draft,
)

ROOT = Path("docs/readiness").resolve()
INPUTS = {
    "provider_questionnaire": ROOT / "phase4d_synthetic_provider_answers.json",
    "license_questionnaire": ROOT / "phase4d_synthetic_license_answers.json",
    "operator_decisions": ROOT / "phase4d_synthetic_operator_answers.json",
    "broker_evidence": ROOT / "phase4d_synthetic_broker_evidence.json",
}
AS_OF = datetime.fromisoformat("2026-09-24T12:00:00+08:00")


def load_raw(name: str) -> dict[str, Any]:
    return json.loads(INPUTS[name].read_text(encoding="utf-8"))


def write_json(path: Path, value: dict[str, Any]) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def create(tmp_path: Path, **overrides: Path) -> ProductionReadinessDraft:
    tmp_path.mkdir(parents=True, exist_ok=True)
    values = INPUTS | overrides
    return create_production_readiness_draft(
        **values,
        output=(tmp_path / "draft.json").resolve(),
        generated_at=AS_OF,
    )


def test_complete_synthetic_intake_validates_but_draft_is_always_no_go(tmp_path: Path) -> None:
    draft = create(tmp_path)
    assert draft.scope == "synthetic_offline"
    assert draft.admission_status == "no_go"
    assert draft.production_admission == "no_go"
    assert draft.live_trading_ready is False
    assert len(draft.broker.blockers) == 8
    assert all(item.status in {"blocked", "partial"} for item in draft.broker.blockers)
    codes = {item.code for item in draft.blockers}
    assert "reconciliation_unknown:unexplained_disagreements" in codes
    assert "reconciliation_unknown:silent_fallbacks" in codes
    assert "reconciliation_unknown:stream_faults" in codes
    assert "reconciliation_unknown:last_common_watermark" in codes
    assert "reconciliation_unknown:rollback_license" in codes
    assert "entitlement_expiry_unknown" in codes
    assert load_draft((tmp_path / "draft.json").resolve()) == draft


def test_production_unknown_answers_remain_no_go(tmp_path: Path) -> None:
    provider = load_raw("provider_questionnaire")
    provider["numeric_latency_sla_milliseconds"] = "UNKNOWN"
    provider["history_start"] = "UNKNOWN"
    provider["history_depth_days"] = "UNKNOWN"
    provider["pit_announcement_revision_semantics"] = "UNKNOWN"
    draft = create(
        tmp_path,
        provider_questionnaire=write_json(tmp_path / "provider.json", provider).resolve(),
    )
    codes = {item.code for item in draft.blockers}
    assert "provider_unknown:numeric_latency_sla_milliseconds" in codes
    assert "provider_unknown:history_start" in codes
    assert "provider_unknown:pit_announcement_revision_semantics" in codes


def test_synthetic_evidence_is_rejected_for_production_candidate(tmp_path: Path) -> None:
    paths: dict[str, Path] = {}
    for name in INPUTS:
        raw = load_raw(name)
        raw["classification"] = "production_candidate"
        paths[name] = write_json(tmp_path / f"{name}.json", raw).resolve()
    with pytest.raises(ValueError, match="synthetic evidence cannot enter"):
        create(tmp_path, **paths)


@pytest.mark.parametrize("field", ["provider_name", "product_name", "product_version"])
def test_missing_provider_product_or_version_fails(tmp_path: Path, field: str) -> None:
    raw = load_raw("provider_questionnaire")
    del raw[field]
    path = write_json(tmp_path / "provider.json", raw).resolve()
    with pytest.raises(ValueError, match=field):
        create(tmp_path, provider_questionnaire=path)


def test_missing_numeric_sla_and_history_fail_schema(tmp_path: Path) -> None:
    raw = load_raw("provider_questionnaire")
    del raw["numeric_latency_sla_milliseconds"]
    del raw["history_start"]
    path = write_json(tmp_path / "provider.json", raw).resolve()
    with pytest.raises(ValueError, match="numeric_latency_sla_milliseconds"):
        create(tmp_path, provider_questionnaire=path)


@pytest.mark.parametrize(
    "url",
    [
        "evidence/document",
        "https://example.invalid/../secret",
        "https://user@example.invalid/x",
        "https://example.invalid/x?token=synthetic",
    ],
)
def test_relative_traversal_and_url_credentials_fail(tmp_path: Path, url: str) -> None:
    raw = load_raw("provider_questionnaire")
    raw["evidence"][0]["source_url"] = url
    path = write_json(tmp_path / "provider.json", raw).resolve()
    with pytest.raises(ValueError, match="evidence source"):
        create(tmp_path, provider_questionnaire=path)


def test_missing_source_checksum_mismatch_and_bad_chronology_fail(tmp_path: Path) -> None:
    missing = load_raw("provider_questionnaire")
    del missing["evidence"][0]["source_url"]
    with pytest.raises(ValueError, match="source_url"):
        create(
            tmp_path,
            provider_questionnaire=write_json(tmp_path / "missing.json", missing).resolve(),
        )

    mismatch = load_raw("provider_questionnaire")
    mismatch["evidence"][0]["verified_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="checksum verification failed"):
        create(
            tmp_path,
            provider_questionnaire=write_json(tmp_path / "mismatch.json", mismatch).resolve(),
        )

    summary = load_raw("provider_questionnaire")
    summary["evidence"][0]["summary"] = "Tampered summary."
    with pytest.raises(ValueError, match="summary checksum failed"):
        create(
            tmp_path,
            provider_questionnaire=write_json(tmp_path / "summary.json", summary).resolve(),
        )

    chronology = load_raw("provider_questionnaire")
    chronology["evidence"][0]["retrieved_on"] = "2026-09-22"
    chronology["evidence"][0]["reviewed_on"] = "2026-09-21"
    with pytest.raises(ValueError, match="invalid review chronology"):
        create(
            tmp_path,
            provider_questionnaire=write_json(tmp_path / "chronology.json", chronology).resolve(),
        )


def test_expired_evidence_and_missing_owner_fail_closed(tmp_path: Path) -> None:
    expired = load_raw("provider_questionnaire")
    expired["evidence"][0]["expires_on"] = "2026-09-23"
    draft = create(
        tmp_path,
        provider_questionnaire=write_json(tmp_path / "expired.json", expired).resolve(),
    )
    assert "expired_evidence:synthetic-provider" in {item.code for item in draft.blockers}

    owner = load_raw("provider_questionnaire")
    owner["owner"]["owner_id"] = "UNKNOWN"
    with pytest.raises(ValueError, match="owner"):
        create(
            tmp_path / "owner-case",
            provider_questionnaire=write_json(tmp_path / "owner.json", owner).resolve(),
        )


def test_unknown_field_and_schema_version_fail_fast(tmp_path: Path) -> None:
    extra = load_raw("provider_questionnaire")
    extra["latest"] = True
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        create(
            tmp_path,
            provider_questionnaire=write_json(tmp_path / "extra.json", extra).resolve(),
        )
    version = load_raw("provider_questionnaire")
    version["schema_version"] = 2
    with pytest.raises(ValueError, match="Input should be 1"):
        create(
            tmp_path,
            provider_questionnaire=write_json(tmp_path / "version.json", version).resolve(),
        )


def test_license_operator_and_broker_unknowns_remain_no_go(tmp_path: Path) -> None:
    legal = load_raw("license_questionnaire")
    for key in ("retention", "encrypted_backup_restore", "derived_use_survival",
                "deletion_certification"):
        legal["decisions"][key]["status"] = "unknown"
        legal["decisions"][key]["answer"] = "UNKNOWN"
        legal["decisions"][key]["evidence_ids"] = []
    operator = load_raw("operator_decisions")
    operator["decisions"]["secret_backend_selection"]["status"] = "unknown"
    broker = load_raw("broker_evidence")
    del broker["blockers"][0]
    with pytest.raises(ValueError, match="all eight semantic blockers"):
        create(
            tmp_path / "broker-case",
            broker_evidence=write_json(tmp_path / "broker.json", broker).resolve(),
        )
    resolved = load_raw("broker_evidence")
    resolved["blockers"][0]["status"] = "resolved"
    with pytest.raises(ValueError, match="blocked"):
        create(
            tmp_path / "resolved-case",
            broker_evidence=write_json(tmp_path / "resolved.json", resolved).resolve(),
        )
    draft = create(
        tmp_path,
        license_questionnaire=write_json(tmp_path / "legal.json", legal).resolve(),
        operator_decisions=write_json(tmp_path / "operator.json", operator).resolve(),
    )
    codes = {item.code for item in draft.blockers}
    assert "license_decision_not_approved:retention" in codes
    assert "license_decision_not_approved:derived_use_survival" in codes
    assert "operator_decision_not_approved:secret_backend_selection" in codes


def test_coverage_below_100_percent_is_no_go(tmp_path: Path) -> None:
    raw = load_raw("provider_questionnaire")
    raw["record_coverage"] = 0.999999
    raw["field_coverage"] = 0.99
    draft = create(
        tmp_path,
        provider_questionnaire=write_json(tmp_path / "coverage.json", raw).resolve(),
    )
    codes = {item.code for item in draft.blockers}
    assert {"coverage_below_100_percent:record", "coverage_below_100_percent:field"} <= codes


def test_comparison_is_deterministic_and_regression_never_improves_admission(
    tmp_path: Path,
) -> None:
    previous = create(tmp_path / "previous")
    raw = previous.model_dump(mode="json")
    raw["evidence"] = raw["evidence"][1:]
    raw["license"]["decisions"]["retention"]["status"] = "unknown"
    raw["metrics"]["critical_record_coverage"] = 0.9
    raw["report_checksum"] = "0" * 64
    candidate = ProductionReadinessDraft.model_validate(raw)
    first = compare_production_readiness(previous, candidate)
    second = compare_production_readiness(previous, candidate)
    assert canonical_json(first) == canonical_json(second)
    assert "evidence_removed:official-shioaji-review" in first.regressions
    assert "decision_regression:license:retention:approved_to_unknown" in first.regressions
    assert "coverage_regression:critical_record_coverage" in first.regressions
    assert first.admission_may_improve is False
    assert first.admission_status == "no_go"


def test_expiry_regression_ordering_and_checksum_are_deterministic(tmp_path: Path) -> None:
    previous = create(tmp_path / "previous")
    raw = previous.model_dump(mode="json")
    raw["generated_at"] = "2027-04-01T12:00:00+08:00"
    candidate = ProductionReadinessDraft.model_validate(raw)
    report = compare_production_readiness(previous, candidate)
    assert report.newly_expired == tuple(sorted(report.newly_expired))
    assert "official-shioaji-review" in report.newly_expired
    payload = report.model_dump(mode="json")
    checksum = payload.pop("report_checksum")
    assert checksum == hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_output_refuses_overwrite_and_cli_does_not_mutate_unrelated_state(tmp_path: Path) -> None:
    output = (tmp_path / "draft.json").resolve()
    runtime = tmp_path / "runtime.db"
    runtime.write_text("unchanged", encoding="utf-8")
    before = hashlib.sha256(runtime.read_bytes()).hexdigest()
    command = [
        ".venv/bin/island-quant", "create-production-readiness-draft",
        "--provider-questionnaire", str(INPUTS["provider_questionnaire"]),
        "--license-questionnaire", str(INPUTS["license_questionnaire"]),
        "--operator-decisions", str(INPUTS["operator_decisions"]),
        "--broker-evidence", str(INPUTS["broker_evidence"]),
        "--output", str(output), "--as-of", AS_OF.isoformat(),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    assert result.returncode == 3
    refused = subprocess.run(command, check=False, capture_output=True, text=True)
    assert refused.returncode == 2 and "--force" in refused.stderr
    assert hashlib.sha256(runtime.read_bytes()).hexdigest() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["draft.json", "runtime.db"]


def test_all_input_output_paths_must_be_explicit_and_absolute(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        create_production_readiness_draft(
            provider_questionnaire=Path("provider.json"),
            license_questionnaire=INPUTS["license_questionnaire"],
            operator_decisions=INPUTS["operator_decisions"],
            broker_evidence=INPUTS["broker_evidence"],
            output=(tmp_path / "draft.json").resolve(),
            generated_at=AS_OF,
        )


def test_checked_in_draft_checksum_and_labels() -> None:
    draft = load_draft((ROOT / "phase4d_synthetic_readiness_draft.json").resolve())
    assert draft.synthetic and draft.offline
    assert draft.production_admission == "no_go"
    assert draft.live_trading_ready is False
    assert all("Synthetic" in item.summary or item.source_kind == "official"
               for item in draft.evidence)


def test_checked_in_phase4d_reports_have_deterministic_checksums() -> None:
    for filename in (
        "phase4d_production_readiness.json",
        "phase4d_synthetic_comparison_report.json",
    ):
        payload = json.loads((ROOT / filename).read_text(encoding="utf-8"))
        checksum = payload.pop("report_checksum")
        assert checksum == hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert payload["admission_status" if "admission_status" in payload else
                       "production_admission"] == "no_go"
        assert payload["live_trading_ready"] is False
