"""Small resilient JSON client shared by provider adapters."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from threading import Lock
from typing import Any


class HttpClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    backoff_seconds: float = 1.0


class RateLimiter:
    """Thread-safe minimum interval guard for rate-sensitive upstream APIs."""

    def __init__(self, minimum_interval_seconds: float):
        self.minimum_interval_seconds = minimum_interval_seconds
        self._lock = Lock()
        self._last_finished_at = 0.0

    def __enter__(self):
        self._lock.acquire()
        remaining = self.minimum_interval_seconds - (time.monotonic() - self._last_finished_at)
        if remaining > 0:
            time.sleep(remaining)
        return self

    def __exit__(self, *_):
        self._last_finished_at = time.monotonic()
        self._lock.release()


class JsonHttpClient:
    def __init__(self, retry: RetryPolicy | None = None):
        self.retry = retry or RetryPolicy()

    def get(self, url: str, headers: dict[str, str] | None = None, timeout: float = 20) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.retry.attempts):
            try:
                request = urllib.request.Request(url, headers=headers or {})
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return json.loads(response.read().decode("utf-8-sig"))
            except urllib.error.HTTPError as exc:
                last_error = exc
                # urllib raises a 30x HTTPError after detecting a redirect loop.
                # CDN redirect state can be transient, so retry 3xx, 408 and 429.
                if 400 <= exc.code < 500 and exc.code not in {408, 429}:
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < self.retry.attempts - 1:
                time.sleep(self.retry.backoff_seconds * (2 ** attempt))
        raise HttpClientError(str(last_error or "unknown HTTP error")) from last_error
