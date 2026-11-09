"""PostgreSQL leases for goal coordination. In-memory locks are not used."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import case, or_, select, update
from sqlalchemy.orm import Session

from apex.capabilities.registry import CapabilityRegistry
from apex.persistence.models import AgentInvocation, Execution, Goal, GoalSubtask
from apex.runtime.transitions import affected_rows

_RECOVERABLE = ("planned", "running")


def claim_goal(
    session: Session,
    goal_id: str,
    owner: str,
    now: datetime,
    lease_seconds: int,
    max_attempts: int,
) -> bool:
    """Take a goal whose lease is free. Reclaiming an expired lease counts as recovery."""
    expiry = now + timedelta(seconds=lease_seconds)
    reclaim = Goal.lease_expires_at.is_not(None)
    result = session.execute(
        update(Goal)
        .where(Goal.id == goal_id)
        .where(Goal.status.in_(_RECOVERABLE))
        .where(or_(Goal.lease_expires_at.is_(None), Goal.lease_expires_at < now))
        .where(or_(Goal.lease_expires_at.is_(None), Goal.recovery_attempts < max_attempts))
        .values(
            lease_owner=owner,
            lease_expires_at=expiry,
            recovery_attempts=case(
                (reclaim, Goal.recovery_attempts + 1),
                else_=Goal.recovery_attempts,
            ),
            updated_at=now,
        )
    )
    return affected_rows(result) == 1


def release_goal(session: Session, goal_id: str, owner: str) -> None:
    """Drop a lease this owner holds. A crash leaves the lease to expire."""
    session.execute(
        update(Goal)
        .where(Goal.id == goal_id, Goal.lease_owner == owner)
        .values(lease_owner=None, lease_expires_at=None)
    )


def recoverable_goal_ids(session: Session, now: datetime, limit: int) -> list[str]:
    """Goals the worker may resume. Terminal, approval, and uncertain goals are absent."""
    rows = session.scalars(
        select(Goal.id)
        .where(Goal.status.in_(_RECOVERABLE))
        .where(or_(Goal.lease_expires_at.is_(None), Goal.lease_expires_at < now))
        .order_by(Goal.updated_at)
        .limit(limit)
    )
    return list(rows)


def exhaust_goal_ids(session: Session, now: datetime, max_attempts: int) -> list[str]:
    """Expired leases that have used their recovery budget."""
    rows = session.scalars(
        select(Goal.id).where(
            Goal.status.in_(_RECOVERABLE),
            Goal.lease_expires_at.is_not(None),
            Goal.lease_expires_at < now,
            Goal.recovery_attempts >= max_attempts,
        )
    )
    return list(rows)


def repair_stale_subtasks(
    session: Session,
    goal_id: str,
    registry: CapabilityRegistry,
    now: datetime,
) -> None:
    """Rebuild a subtask from its execution row after a coordinator dies mid-step."""
    rows = list(session.scalars(select(GoalSubtask).where(GoalSubtask.goal_id == goal_id)))
    for subtask in rows:
        if subtask.status != "running":
            continue
        execution = session.get(Execution, subtask.execution_id) if subtask.execution_id else None
        if execution is not None and execution.status == "succeeded":
            subtask.status = "completed"
            subtask.output_payload = dict(execution.output_payload or {})
            subtask.error_code = None
            _close_running(session, subtask.id, "completed", now)
        elif execution is not None and (
            execution.status == "uncertain"
            or execution.outcome_certainty in {"possibly_sent", "inconclusive"}
        ):
            subtask.status = "uncertain"
            subtask.error_code = "outcome_uncertain"
            _close_running(session, subtask.id, "uncertain", now)
        elif execution is None:
            spec = registry.get(subtask.capability_id or "")
            if (
                spec is not None
                and spec.idempotent
                and subtask.attempt_count < spec.max_attempts + 1
            ):
                subtask.status = "pending"
                subtask.error_code = None
                _close_running(session, subtask.id, "failed", now)
            else:
                subtask.status = "failed"
                subtask.error_code = "stale_invocation"
                _close_running(session, subtask.id, "failed", now)
        else:
            subtask.status = "failed"
            subtask.error_code = execution.error_code or "execution_failed"
            _close_running(session, subtask.id, "failed", now)
        subtask.updated_at = now


def _close_running(session: Session, subtask_id: str, status: str, now: datetime) -> None:
    rows = session.scalars(
        select(AgentInvocation).where(
            AgentInvocation.subtask_id == subtask_id,
            AgentInvocation.status == "running",
        )
    )
    for row in rows:
        row.status = status
        row.error_code = "stale_invocation" if status != "completed" else None
        row.finished_at = now
