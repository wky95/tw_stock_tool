"""Immutable, pinned paper daily report writer."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PaperDailyReport:
    schema_version: int
    environment: str
    session: str
    generated_at: datetime
    data_freshness: str
    strategy_version: str
    model_version: str
    artifact_version: str
    order_count: int
    fill_count: int
    rejection_count: int
    positions: tuple[tuple[str, int], ...]
    cash: Decimal
    nav: Decimal
    daily_pnl: Decimal
    costs: Decimal
    gross_exposure: Decimal
    risk_events: tuple[str, ...]
    reconciliation_status: str
    alert_count: int
    safe_mode: bool
    data_quality_issues: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.environment != "paper":
            raise ValueError("daily report must be paper")


def write_daily_report(root: Path, report: PaperDailyReport) -> str:
    payload = json.dumps(asdict(report), default=str, sort_keys=True, separators=(",", ":"))
    version = hashlib.sha256(payload.encode()).hexdigest()
    destination = root / "paper-reports" / version / "report.json"
    if destination.exists():
        if destination.read_text(encoding="utf-8") != payload:
            raise RuntimeError("immutable report version collision")
        return version
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".report-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return version
