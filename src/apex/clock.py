"""Clock abstraction so expiry and leases can be tested without sleeping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


def utcnow() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


def ensure_aware(value: datetime) -> datetime:
    """Treat naive timestamps from SQLite as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class Clock(Protocol):
    """Anything that can report the current time."""

    def now(self) -> datetime:
        """Return the current instant."""


class SystemClock:
    """Production clock."""

    def now(self) -> datetime:
        return utcnow()


class FrozenClock:
    """Clock fixed at one instant, used by tests."""

    def __init__(self, instant: datetime | None = None) -> None:
        self._instant = instant or utcnow()

    def now(self) -> datetime:
        return self._instant

    def advance(self, delta: timedelta) -> None:
        self._instant = self._instant + delta
