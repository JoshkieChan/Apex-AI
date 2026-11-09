"""Explicit queries. Callers own transaction boundaries."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from apex.contracts.schemas import IDLE_TASK_STATUSES
from apex.persistence.models import (
    Approval,
    ContextRecord,
    Execution,
    PermissionGrant,
    Plan,
    PlanStep,
    Principal,
    Task,
    TaskEvent,
    Verification,
)


def get_principal_by_key(session: Session, external_key: str) -> Principal | None:
    return session.scalar(select(Principal).where(Principal.external_key == external_key))


def get_principal(session: Session, principal_id: str) -> Principal | None:
    return session.get(Principal, principal_id)


def get_grant(session: Session, principal_id: str, permission_key: str) -> PermissionGrant | None:
    return session.scalar(
        select(PermissionGrant).where(
            PermissionGrant.principal_id == principal_id,
            PermissionGrant.permission_key == permission_key,
        )
    )


def get_task(session: Session, task_id: str) -> Task | None:
    return session.get(Task, task_id)


def find_task_by_idempotency(
    session: Session, principal_id: str, idempotency_key: str
) -> Task | None:
    return session.scalar(
        select(Task).where(
            Task.principal_id == principal_id,
            Task.idempotency_key == idempotency_key,
        )
    )


def latest_plan(session: Session, task_id: str) -> Plan | None:
    return session.scalar(
        select(Plan).where(Plan.task_id == task_id).order_by(Plan.version.desc()).limit(1)
    )


def steps_for_plan(session: Session, plan_id: str) -> list[PlanStep]:
    return list(
        session.scalars(
            select(PlanStep).where(PlanStep.plan_id == plan_id).order_by(PlanStep.step_index.asc())
        )
    )


def approvals_for_task(session: Session, task_id: str) -> list[Approval]:
    return list(session.scalars(select(Approval).where(Approval.task_id == task_id)))


def get_approval(session: Session, approval_id: str) -> Approval | None:
    return session.get(Approval, approval_id)


def approval_for_step_hash(
    session: Session, plan_step_id: str, operation_hash: str
) -> Approval | None:
    return session.scalar(
        select(Approval).where(
            Approval.plan_step_id == plan_step_id,
            Approval.operation_hash == operation_hash,
        )
    )


def executions_for_task(session: Session, task_id: str) -> list[Execution]:
    return list(
        session.scalars(
            select(Execution)
            .where(Execution.task_id == task_id)
            .order_by(Execution.started_at.asc())
        )
    )


def executions_for_step(session: Session, plan_step_id: str) -> list[Execution]:
    return list(
        session.scalars(
            select(Execution)
            .where(Execution.plan_step_id == plan_step_id)
            .order_by(Execution.attempt.asc())
        )
    )


def succeeded_execution(session: Session, plan_step_id: str) -> Execution | None:
    return session.scalar(
        select(Execution).where(
            Execution.plan_step_id == plan_step_id,
            Execution.status == "succeeded",
        )
    )


def verifications_for_task(session: Session, task_id: str) -> list[Verification]:
    return list(session.scalars(select(Verification).where(Verification.task_id == task_id)))


def verification_for_execution(session: Session, execution_id: str) -> Verification | None:
    return session.scalar(select(Verification).where(Verification.execution_id == execution_id))


def events_for_task(session: Session, task_id: str, limit: int = 100) -> list[TaskEvent]:
    return list(
        session.scalars(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id)
            .order_by(TaskEvent.created_at.asc())
            .limit(limit)
        )
    )


def context_for_scope(session: Session, scope_type: str, scope_id: str) -> list[ContextRecord]:
    return list(
        session.scalars(
            select(ContextRecord).where(
                ContextRecord.scope_type == scope_type,
                ContextRecord.scope_id == scope_id,
            )
        )
    )


def recoverable_task_ids(session: Session, now: datetime) -> list[str]:
    """Non-terminal tasks whose lease has expired and can be claimed again."""
    rows = session.scalars(
        select(Task.id).where(
            Task.status.notin_(tuple(status.value for status in IDLE_TASK_STATUSES)),
            Task.lease_expires_at.is_not(None),
            Task.lease_expires_at < now,
        )
    )
    return list(rows)
