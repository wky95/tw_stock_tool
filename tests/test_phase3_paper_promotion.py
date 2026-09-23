from __future__ import annotations

import hashlib
from argparse import Namespace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from island_quant.config import AppSettings, ResearchSettings
from island_quant.operations.cli import run_promotion
from island_quant.operations.promotion import PaperPromotionRequest, PaperTargetPromoter
from island_quant.operations.strategy import REQUIRED_LINEAGE, ExactPaperTargetReader
from island_quant.pipeline.artifacts import ExactArtifactStore

DECISION = datetime(2025, 1, 1, 8, tzinfo=UTC)
APPROVED = datetime(2025, 1, 1, 12, tzinfo=UTC)
SESSION = date(2025, 1, 2)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def source(
    root: Path,
    *,
    completeness: str = "validated",
    classification: str = "validated_research_candidate",
    lineage: dict[str, str] | None = None,
) -> str:
    manifest = ExactArtifactStore(root, "research_target_candidates", 1).publish(
        [
            {
                "instrument_id": "2330",
                "market": "TWSE",
                "execution_session": SESSION.isoformat(),
                "decision_time": DECISION.isoformat(),
                "available_at": datetime(2025, 1, 1, 7, tzinfo=UTC).isoformat(),
                "target_weight": "0.10",
                "reference_price": "10",
                "eligible": True,
            }
        ],
        lineage=lineage or {name: digest(name) for name in REQUIRED_LINEAGE},
        completeness=completeness,
        classification=classification,
        created_at=DECISION,
    )
    return manifest.artifact_version


def request(version: str, **overrides: object) -> PaperPromotionRequest:
    values: dict[str, object] = {
        "source_version": version,
        "reviewer": "paper-operator",
        "reason": "offline engineering review passed",
        "approved_at": APPROVED,
        "execution_session": SESSION,
        "pit_reviewed": True,
        "data_license_reviewed": True,
        "risk_reviewed": True,
    }
    values.update(overrides)
    return PaperPromotionRequest(**values)  # type: ignore[arg-type]


def test_dry_run_validates_without_publishing_and_confirm_is_idempotent(tmp_path: Path) -> None:
    version = source(tmp_path)
    promoter = PaperTargetPromoter(tmp_path)
    preview = promoter.promote(request(version), dry_run=True)
    assert preview.dry_run is True
    assert preview.record_count == 1
    assert preview.approval_version is None and preview.paper_target_version is None
    assert not (tmp_path / "paper_promotion_approvals").exists()
    first = promoter.promote(request(version), dry_run=False)
    second = promoter.promote(request(version), dry_run=False)
    assert first == second
    assert first.approval_version is not None
    assert first.paper_target_version is not None
    snapshot = ExactPaperTargetReader(tmp_path).read(
        first.paper_target_version, session=SESSION, as_of=APPROVED
    )
    assert snapshot.targets[0].instrument_key == "TWSE:2330"
    assert dict(snapshot.lineage)["promotion_approval_version"] == first.approval_version


@pytest.mark.parametrize(
    "overrides,error",
    [
        ({"pit_reviewed": False}, "checklist"),
        ({"data_license_reviewed": False}, "checklist"),
        ({"risk_reviewed": False}, "checklist"),
        (
            {"approved_at": datetime(2024, 12, 31, tzinfo=UTC)},
            "predates",
        ),
        (
            {"approved_at": datetime(2025, 1, 2, 1, tzinfo=UTC)},
            "precede the execution session",
        ),
    ],
)
def test_promotion_fails_closed_before_writing(
    tmp_path: Path, overrides: dict[str, object], error: str
) -> None:
    version = source(tmp_path)
    with pytest.raises(RuntimeError, match=error):
        PaperTargetPromoter(tmp_path).promote(request(version, **overrides), dry_run=False)
    assert not (tmp_path / "paper_promotion_approvals").exists()


def test_promotion_rejects_exploratory_and_incomplete_lineage(tmp_path: Path) -> None:
    exploratory = source(tmp_path, completeness="exploratory")
    with pytest.raises(RuntimeError, match="not eligible"):
        PaperTargetPromoter(tmp_path).promote(request(exploratory), dry_run=False)
    incomplete = source(
        tmp_path / "incomplete", lineage={"strategy_version": digest("strategy")}
    )
    with pytest.raises(RuntimeError, match="lineage is incomplete"):
        PaperTargetPromoter(tmp_path / "incomplete").promote(
            request(incomplete), dry_run=False
        )


def test_promotion_rejects_noncanonical_source_version(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="canonical"):
        request("latest")
    with pytest.raises(ValueError, match="canonical"):
        request("../escape")


def test_promotion_rejects_corrupted_source_before_approval_write(tmp_path: Path) -> None:
    version = source(tmp_path)
    records = tmp_path / "research_target_candidates" / version / "records.jsonl"
    records.write_text(records.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="checksum"):
        PaperTargetPromoter(tmp_path).promote(request(version), dry_run=False)
    assert not (tmp_path / "paper_promotion_approvals").exists()


def test_cli_requires_explicit_confirmation_and_supports_safe_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    version = source(tmp_path)
    settings = AppSettings(research=ResearchSettings(artifact_root=tmp_path))
    base = {
        "source_version": version,
        "reviewer": "reviewer",
        "reason": "reviewed",
        "approved_at": APPROVED.isoformat(),
        "execution_session": SESSION.isoformat(),
        "approve_pit": True,
        "approve_data_license": True,
        "approve_risk": True,
        "confirm": False,
    }
    assert run_promotion(Namespace(**base, dry_run=False), settings) == 2
    assert "--dry-run or --confirm" in capsys.readouterr().err
    assert run_promotion(Namespace(**base, dry_run=True), settings) == 0
    assert '"dry_run": true' in capsys.readouterr().out
    assert not (tmp_path / "paper_promotion_approvals").exists()
