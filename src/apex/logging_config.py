"""Structured JSON logging with secret redaction."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from apex.config import Settings
from apex.redaction import redact

_SAFE_FIELDS = (
    "request_id",
    "task_id",
    "principal_id",
    "capability_id",
    "decision",
    "event",
)


class JsonFormatter(logging.Formatter):
    """Format log records as one JSON object per line, after redaction."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for field in _SAFE_FIELDS:
            value = getattr(record, field, None)
            if value:
                payload[field] = value
        if record.exc_info:
            payload["exception_type"] = (
                record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
            )
        return json.dumps(payload, default=str)


def configure_logging(settings: Settings) -> None:
    """Attach one redacting JSON handler to the apex logger."""
    logger = logging.getLogger("apex")
    logger.handlers.clear()
    logger.setLevel(settings.log_level.upper())
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    # Driver logs can include a database URL. Apex already records the exception type.
    for name in ("sqlalchemy", "sqlalchemy.engine", "sqlalchemy.pool"):
        driver = logging.getLogger(name)
        driver.handlers.clear()
        driver.propagate = False
        driver.addFilter(_drop)


def _drop(_record: logging.LogRecord) -> bool:
    return False


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the apex namespace."""
    return logging.getLogger(f"apex.{name}")
