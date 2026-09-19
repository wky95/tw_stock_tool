"""Machine-readable point-in-time coverage and promotion blockers."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

from island_quant.pipeline.artifacts import canonical_json


@dataclass(frozen=True, slots=True)
class CoverageReport:
    date_coverage: str
    instrument_coverage: str
    unknown_market: int
    provisional_listing_date: int
    missing_delisted_history: bool
    corporate_action_coverage: str
    suspension_coverage: str
    price_limit_tradability_coverage: str
    benchmark_coverage: str
    market_cap_coverage: str
    feature_validity: str
    label_validity: str
    prediction_coverage: str
    backtest_valuation_completeness: str
    pit_research_completeness: str
    promotion_blockers: tuple[str, ...]
    checksum: str = ""

    @classmethod
    def create(cls, **values: object) -> CoverageReport:
        checksum = hashlib.sha256(canonical_json({**values, "checksum": ""})).hexdigest()
        return cls(checksum=checksum, **values)  # type: ignore[arg-type]

    def verify(self) -> None:
        expected = hashlib.sha256(canonical_json({**asdict(self), "checksum": ""})).hexdigest()
        if expected != self.checksum:
            raise RuntimeError("coverage report checksum mismatch")
