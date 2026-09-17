"""Structured logging configuration for services and command-line jobs."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from island_quant.config import LoggingSettings


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line with stable operational fields."""

    _reserved = set(logging.makeLogRecord({}).__dict__)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._reserved and key not in {"message", "asctime"}:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(settings: LoggingSettings) -> None:
    """Configure the process root logger exactly once per invocation."""
    handler = logging.StreamHandler()
    if settings.json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.level.upper())
