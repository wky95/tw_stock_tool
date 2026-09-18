"""Provider-neutral contracts for point-in-time market data ingestion."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast


@dataclass(frozen=True, slots=True)
class DataRequest:
    dataset: str
    data_id: str | None = None
    start_date: str | None = None
    end_date: str | None = None

    @property
    def key(self) -> str:
        encoded = json.dumps(
            {
                "dataset": self.dataset,
                "data_id": self.data_id,
                "start_date": self.start_date,
                "end_date": self.end_date,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderPayload:
    provider: str
    request: DataRequest
    fetched_at: datetime
    raw_body: bytes

    def rows(self) -> list[dict[str, Any]]:
        parsed = json.loads(self.raw_body)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("data"), list):
            raise ValueError(f"{self.request.dataset} response does not contain a data list")
        status = parsed.get("status")
        if status not in (None, 200):
            raise ValueError(f"{self.request.dataset} provider status is {status!r}")
        rows = parsed["data"]
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError(f"{self.request.dataset} contains a non-object row")
        return cast(list[dict[str, Any]], rows)


class HistoricalDataProvider(Protocol):
    name: str

    def fetch(self, request: DataRequest) -> ProviderPayload:
        """Fetch an exact raw provider response without normalizing it."""
        ...
