"""Compare-and-swap task transitions and audit events."""

from __future__ import annotations

from typing import Any

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from apex.clock import Clock
from apex.contracts.schemas import LEGAL_TRANSITIONS, TaskStatus
from apex.errors import DomainError
from apex.persistence.models import Task, TaskEvent
from apex.runtime_ids import new_id


def transition(
    session: Session,
    task: Task,
    to_status: TaskStatus,
    *,
    event_type: str,
    actor_id: str | None,
    payload: dict[str, Any],
    clock: Clock,
    extra: dict[str, Any] | None = None,
) -> None:
    """Move a task when the current status still matches. The caller commits."""
    current = TaskStatus(task.status)
    if to_status not in LEGAL_TRANSITIONS[current]:
        raise DomainError("illegal_transition", "The task cannot move to that state.")
    now = clock.now()
    values: dict[str, Any] = {"status": to_status.value, "updated_at": now}
    if extra:
        values.update(extra)
    result = session.execute(
        update(Task).where(Task.id == task.id, Task.status == current.value).values(**values)
    )
    if affected_rows(result) != 1:
        raise DomainError("conflict", "The task state changed.")
    task.status = to_status.value
    task.updated_at = now
    for key, value in (extra or {}).items():
        setattr(task, key, value)
    session.add(
        TaskEvent(
            id=new_id(),
            task_id=task.id,
            event_type=event_type,
            from_status=current.value,
            to_status=to_status.value,
            actor_principal_id=actor_id,
            payload=payload,
            created_at=now,
        )
    )


def affected_rows(result: object) -> int:
    """Rows changed by DML. Session.execute is typed as Result, which has no rowcount."""
    if not isinstance(result, CursorResult):
        return -1
    count = result.rowcount
    if not isinstance(count, int):
        return -1
    return count
