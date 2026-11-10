"""Execution hold. New capability work stops. Existing outcomes stay where they are."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from apex.logging_config import get_logger
from apex.persistence.models import RuntimeControl

logger = get_logger("operations")

EXECUTION_SUSPENDED = "execution_suspended"


def execution_suspended(session: Session) -> bool:
    """Return whether new capability execution is held."""
    row = session.get(RuntimeControl, EXECUTION_SUSPENDED)
    return bool(row is not None and row.enabled)


def set_execution_suspended(
    session: Session,
    *,
    enabled: bool,
    actor_id: str,
    now: datetime,
) -> bool:
    """Record the hold. The caller has already been authorized."""
    row = session.get(RuntimeControl, EXECUTION_SUSPENDED)
    if row is None:
        row = RuntimeControl(
            control_key=EXECUTION_SUSPENDED,
            enabled=enabled,
            updated_at=now,
            updated_by=actor_id,
        )
        session.add(row)
    else:
        row.enabled = enabled
        row.updated_at = now
        row.updated_by = actor_id
    session.commit()
    logger.info(
        "execution hold updated",
        extra={"outcome": "suspended" if enabled else "resumed", "principal_id": actor_id},
    )
    return bool(row.enabled)
