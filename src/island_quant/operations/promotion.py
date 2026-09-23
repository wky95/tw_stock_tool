"""Explicit, immutable promotion from validated research targets to PAPER candidates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from island_quant.operations.strategy import (
    MARKET_TIMEZONE,
    REQUIRED_LINEAGE,
    validate_paper_target_records,
)
from island_quant.pipeline.artifacts import (
    ArtifactManifest,
    ExactArtifactStore,
    require_exact_version,
)


@dataclass(frozen=True, slots=True)
class PaperPromotionRequest:
    source_version: str
    reviewer: str
    reason: str
    approved_at: datetime
    execution_session: date
    pit_reviewed: bool
    data_license_reviewed: bool
    risk_reviewed: bool
    policy_version: str = "paper-target-promotion-v1"

    def __post_init__(self) -> None:
        require_exact_version(self.source_version)
        if self.approved_at.tzinfo is None or self.approved_at.utcoffset() is None:
            raise ValueError("paper promotion approval timestamp must be timezone-aware")
        if not self.reviewer.strip() or not self.reason.strip():
            raise ValueError("paper promotion requires reviewer and reason")
        if not self.policy_version or self.policy_version in {"latest", "current"}:
            raise ValueError("paper promotion policy requires a pinned version")


@dataclass(frozen=True, slots=True)
class PaperPromotionResult:
    source_version: str
    approval_version: str | None
    paper_target_version: str | None
    record_count: int
    dry_run: bool


class PaperTargetPromoter:
    source_type = "research_target_candidates"
    approval_type = "paper_promotion_approvals"
    target_type = "paper_target_snapshots"
    schema_version = 1

    def __init__(self, root: Path) -> None:
        self.source = ExactArtifactStore(root, self.source_type, self.schema_version)
        self.approvals = ExactArtifactStore(root, self.approval_type, self.schema_version)
        self.targets = ExactArtifactStore(root, self.target_type, self.schema_version)

    def promote(
        self, request: PaperPromotionRequest, *, dry_run: bool
    ) -> PaperPromotionResult:
        manifest, records = self._validate(request)
        if dry_run:
            return PaperPromotionResult(
                request.source_version, None, None, len(records), True
            )
        approval = self.approvals.publish(
            [
                {
                    **asdict(request),
                    "approved_at": request.approved_at.isoformat(),
                    "execution_session": request.execution_session.isoformat(),
                    "source_checksum": manifest.data_checksum,
                    "source_record_count": manifest.record_count,
                }
            ],
            lineage={"source_target_version": request.source_version},
            completeness="validated",
            classification="paper_promotion_approval",
            created_at=request.approved_at,
        )
        output_lineage = dict(manifest.lineage)
        output_lineage["promotion_approval_version"] = approval.artifact_version
        target = self.targets.publish(
            records,
            lineage=output_lineage,
            completeness="validated",
            classification="paper_candidate",
            created_at=request.approved_at,
        )
        return PaperPromotionResult(
            request.source_version,
            approval.artifact_version,
            target.artifact_version,
            len(records),
            False,
        )

    def _validate(
        self, request: PaperPromotionRequest
    ) -> tuple[ArtifactManifest, list[dict[str, object]]]:
        if not (
            request.pit_reviewed
            and request.data_license_reviewed
            and request.risk_reviewed
        ):
            raise RuntimeError("paper promotion checklist is incomplete")
        manifest = self.source.manifest(request.source_version)
        if (
            manifest.completeness != "validated"
            or manifest.classification != "validated_research_candidate"
        ):
            raise RuntimeError("research target is not eligible for paper promotion")
        if manifest.created_at.tzinfo is None or manifest.created_at.utcoffset() is None:
            raise RuntimeError("research target creation timestamp must be timezone-aware")
        if manifest.created_at > request.approved_at:
            raise RuntimeError("paper approval predates the research target")
        if request.approved_at.astimezone(MARKET_TIMEZONE).date() >= (
            request.execution_session
        ):
            raise RuntimeError("paper approval must precede the execution session")
        lineage = dict(manifest.lineage)
        missing = REQUIRED_LINEAGE - lineage.keys()
        if missing:
            raise RuntimeError(
                f"research target lineage is incomplete: {','.join(sorted(missing))}"
            )
        records = [
            record
            for batch in self.source.batches(request.source_version)
            for record in batch.records
        ]
        validate_paper_target_records(
            records,
            session=request.execution_session,
            as_of=request.approved_at,
        )
        return manifest, records
