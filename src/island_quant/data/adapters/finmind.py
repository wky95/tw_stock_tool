"""FinMind HTTP adapter. Provider response types do not cross this boundary."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

from island_quant.data.ports import DataRequest, ProviderPayload


class FinMindError(RuntimeError):
    """Raised after a FinMind request cannot be completed safely."""


class FinMindProvider:
    name = "finmind"
    endpoint = "https://api.finmindtrade.com/api/v4/data"

    def __init__(
        self,
        token: str | None = None,
        attempts: int = 4,
        timeout_seconds: float = 30,
        base_delay_seconds: float = 0.5,
    ) -> None:
        self._token = token
        self._attempts = attempts
        self._timeout = timeout_seconds
        self._base_delay = base_delay_seconds

    def fetch(self, request: DataRequest) -> ProviderPayload:
        parameters = {"dataset": request.dataset}
        if request.data_id:
            parameters["data_id"] = request.data_id
        if request.start_date:
            parameters["start_date"] = request.start_date
        if request.end_date:
            parameters["end_date"] = request.end_date
        url = f"{self.endpoint}?{urllib.parse.urlencode(parameters)}"
        headers = {"Accept": "application/json", "User-Agent": "island-quant/0.1"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        last_error: Exception | None = None
        for attempt in range(self._attempts):
            retry_delay = self._base_delay * (2**attempt)
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=self._timeout
                ) as response:
                    raw_body = response.read()
                decoded = json.loads(raw_body)
                if decoded.get("status") != 200:
                    raise FinMindError(
                        f"FinMind rejected {request.dataset}: status={decoded.get('status')!r}"
                    )
                return ProviderPayload(self.name, request, datetime.now(UTC), raw_body)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {408, 429} and 400 <= exc.code < 500:
                    raise FinMindError(
                        f"FinMind rejected {request.dataset}: HTTP {exc.code}"
                    ) from exc
                if exc.code == 429:
                    retry_after = exc.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        retry_delay = max(retry_delay, float(retry_after))
            except (TimeoutError, urllib.error.URLError, json.JSONDecodeError, FinMindError) as exc:
                last_error = exc
            if attempt + 1 < self._attempts:
                time.sleep(retry_delay)
        raise FinMindError(f"FinMind request failed for {request.dataset}") from last_error
