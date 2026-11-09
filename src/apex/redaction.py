"""Redact credential-shaped strings before they reach logs or client errors."""

from __future__ import annotations

import re

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(
        r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*([^\s,;]+)",
    ),
    re.compile(r"(://[^:/\s]+:)([^@/\s]+)(@)"),
)

_REDACTED = "[REDACTED]"


def redact(value: str) -> str:
    """Return text with credential-shaped substrings replaced."""
    redacted = value
    redacted = _PATTERNS[0].sub(_REDACTED, redacted)
    redacted = _PATTERNS[1].sub(_REDACTED, redacted)
    redacted = _PATTERNS[2].sub("Bearer [REDACTED]", redacted)
    redacted = _PATTERNS[3].sub(lambda match: f"{match.group(1)}={_REDACTED}", redacted)
    redacted = _PATTERNS[4].sub(
        lambda match: f"{match.group(1)}{_REDACTED}{match.group(3)}", redacted
    )
    return redacted


def safe_identifier(value: str, *, pattern: str = r"[A-Za-z0-9_.:-]{1,128}") -> str:
    """Return the identifier when it matches the allowed pattern, otherwise a placeholder."""
    if re.fullmatch(pattern, value):
        return value
    return "[invalid]"
