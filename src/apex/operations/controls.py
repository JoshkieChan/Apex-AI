"""Execution hold. New capability work stops. Existing outcomes stay where they are."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from apex.clock import ensure_aware
from apex.logging_config import get_logger
from apex.persistence.models import RuntimeControl

logger = get_logger("operations")

EXECUTION_SUSPENDED = "execution_suspended"


def execution_suspended(session: Session) -> bool:
    """Return whether new capability execution is held."""
    row = session.get(RuntimeControl, EXECUTION_SUSPENDED)
    return bool(row is not None and row.enabled)


def claim_execution_slot(session: Session) -> bool:
    """Lock the hold row and return whether a new attempt may be reserved.

    The lock is held until this session commits or rolls back. A hold request
    waits for that transaction. After the hold commits, the next claim sees it
    and must not insert an execution or consume an approval.
    """
    row = _locked_gate(session)
    return not row.enabled


def set_execution_suspended(
    session: Session,
    *,
    enabled: bool,
    actor_id: str,
    now: datetime,
) -> bool:
    """Record the hold. The caller has already been authorized."""
    row = _locked_gate(session)
    row.enabled = enabled
    row.updated_at = now
    row.updated_by = actor_id
    session.commit()
    logger.info(
        "execution hold updated",
        extra={"outcome": "suspended" if enabled else "resumed", "principal_id": actor_id},
    )
    return bool(row.enabled)


def _locked_gate(session: Session) -> RuntimeControl:
    """Return the hold row, creating it if needed, and lock it on PostgreSQL."""
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        session.execute(
            text(
                "INSERT INTO runtime_controls "
                "(control_key, enabled, updated_at, updated_by) "
                "VALUES (:key, false, NOW(), 'system') "
                "ON CONFLICT (control_key) DO NOTHING"
            ),
            {"key": EXECUTION_SUSPENDED},
        )
        row = session.scalar(
            select(RuntimeControl)
            .where(RuntimeControl.control_key == EXECUTION_SUSPENDED)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise RuntimeError("The execution hold row was not locked.")
        return row
    row = session.get(RuntimeControl, EXECUTION_SUSPENDED)
    if row is None:
        row = RuntimeControl(
            control_key=EXECUTION_SUSPENDED,
            enabled=False,
            updated_at=ensure_aware(datetime.now(UTC)),
            updated_by="system",
        )
        session.add(row)
        session.flush()
        return row
    session.refresh(row)
    return row
