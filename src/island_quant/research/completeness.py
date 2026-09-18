"""Research-quality classification and validated-promotion gate."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ResearchCompleteness:
    universe_complete: bool
    unknown_market_count: int
    provisional_listing_date_count: int
    unsupported_corporate_action_count: int
    missing_suspension_status_count: int
    dataset_version: str
    feature_set_version: str
    label_version: str
    availability_policy_version: str

    @property
    def classification(self) -> str:
        complete = (
            self.universe_complete
            and self.unknown_market_count == 0
            and self.provisional_listing_date_count == 0
            and self.unsupported_corporate_action_count == 0
            and self.missing_suspension_status_count == 0
        )
        return "validated" if complete else "exploratory"

    def require_validated(self) -> None:
        if self.classification != "validated":
            raise RuntimeError("validated factor promotion rejected: PIT reference data incomplete")

    def to_metadata(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "is_research_complete": self.universe_complete,
            "research_quality_classification": self.classification,
        }
