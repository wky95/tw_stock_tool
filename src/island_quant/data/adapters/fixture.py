"""Offline provider used by deterministic CLI and integration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from island_quant.data.ports import DataRequest, ProviderPayload


class FixtureProvider:
    name = "fixture"

    def __init__(self, root: Path, fetched_at: datetime | None = None) -> None:
        self.root = root
        self.fetched_at = fetched_at or datetime(2024, 1, 11, 10, tzinfo=UTC)
        self.fetch_count = 0

    def fetch(self, request: DataRequest) -> ProviderPayload:
        suffix = f"__{request.data_id}" if request.data_id else ""
        path = self.root / f"{request.dataset}{suffix}.json"
        if not path.exists():
            raise FileNotFoundError(f"fixture response not found: {path}")
        self.fetch_count += 1
        return ProviderPayload(self.name, request, self.fetched_at, path.read_bytes())
