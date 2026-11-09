"""Approval lifecycle bound to an exact operation hash and payload."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from apex.authority.engine import AuthorityEngine
from apex.clock import ensure_aware
from apex.contracts.schemas import ApprovalStatus
from apex.errors import DomainError
from apex.persistence.models import Approval, PlanStep
from apex.persistence.repository import approval_for_step_hash, get_approval
from apex.runtime_ids import new_id

APPROVE_PERMISSION = "authority.approve"


class ApprovalService:
    """Create and decide approvals. The requester cannot approve their own operation."""

    def __init__(self, session: Session, authority: AuthorityEngine, ttl_hours: int) -> None:
        self._session = session
        self._authority = authority
        self._ttl_hours = ttl_hours

    def ensure_pending(
        self,
        *,
        task_id: str,
        plan_id: str,
        step: PlanStep,
        requested_by: str,
        now: datetime,
    ) -> Approval:
        existing = approval_for_step_hash(self._session, step.id, step.operation_hash)
        if existing is not None:
            return existing
        approval = Approval(
            id=new_id(),
            task_id=task_id,
            plan_id=plan_id,
            plan_step_id=step.id,
            capability_id=step.capability_id,
            operation_hash=step.operation_hash,
            canonical_payload=_as_object(step.input_payload),
            canonical_bindings=_as_object(step.input_bindings),
            status=ApprovalStatus.pending.value,
            requested_by_principal_id=requested_by,
            decided_by_principal_id=None,
            reason=None,
            expires_at=now + timedelta(hours=self._ttl_hours),
            decided_at=None,
            created_at=now,
        )
        self._session.add(approval)
        self._session.flush()
        return approval

    def decide(
        self,
        *,
        approver_id: str,
        approval_id: str,
        decision: str,
        reason: str,
        now: datetime,
    ) -> Approval:
        approval = get_approval(self._session, approval_id)
        if approval is None:
            raise DomainError("approval_not_found", "Approval not found.")
        if approver_id == approval.requested_by_principal_id:
            raise DomainError(
                "separation_of_duties",
                "The requesting principal cannot approve their own operation.",
            )
        step = self._session.get(PlanStep, approval.plan_step_id)
        if step is None or not _operation_matches(approval, step):
            raise DomainError(
                "approval_mismatch",
                "The operation no longer matches the approval request.",
            )
        if not self._authority.can_approve(approver_id, step.permission_key, step.risk, now):
            raise DomainError("approver_not_authorized", "This principal cannot decide approvals.")
        if approval.status != ApprovalStatus.pending.value:
            raise DomainError("approval_already_decided", "This approval is no longer pending.")
        if ensure_aware(approval.expires_at) <= now:
            approval.status = ApprovalStatus.expired.value
            approval.decided_at = now
            self._session.flush()
            raise DomainError("approval_expired", "This approval has expired.")
        if decision not in {ApprovalStatus.approved.value, ApprovalStatus.rejected.value}:
            raise DomainError("invalid_decision", "Approval decision is not valid.")
        approval.status = decision
        approval.decided_by_principal_id = approver_id
        approval.decided_at = now
        approval.reason = reason
        self._session.flush()
        return approval

    def cancel_pending_for_task(self, task_id: str, now: datetime) -> None:
        approvals = self._session.scalars(select(Approval).where(Approval.task_id == task_id))
        for approval in approvals:
            if approval.status == ApprovalStatus.pending.value:
                approval.status = ApprovalStatus.cancelled.value
                approval.decided_at = now
        self._session.flush()


def _operation_matches(approval: Approval, step: PlanStep) -> bool:
    return (
        step.operation_hash == approval.operation_hash
        and _as_object(step.input_payload) == _as_object(approval.canonical_payload)
        and _as_object(step.input_bindings) == _as_object(approval.canonical_bindings)
        and step.capability_id == approval.capability_id
    )


def _as_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}
