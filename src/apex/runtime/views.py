"""Assemble an evidence-backed task view from persisted rows."""

from __future__ import annotations

from sqlalchemy.orm import Session

from apex.contracts.schemas import (
    ApprovalStatus,
    ApprovalView,
    CheckResult,
    ExecutionStatus,
    ExecutionView,
    OutcomeKind,
    PlanStepView,
    PlanView,
    RiskLevel,
    TaskErrorView,
    TaskEventView,
    TaskStatus,
    TaskStatusView,
    TaskView,
    VerificationStatus,
    VerificationView,
)
from apex.persistence.models import Task
from apex.persistence.repository import (
    approvals_for_task,
    events_for_task,
    executions_for_task,
    latest_plan,
    steps_for_plan,
    verifications_for_task,
)


def task_view(session: Session, task: Task) -> TaskView:
    """Build the API representation of one task."""
    plan_row = latest_plan(session, task.id)
    plan = None
    if plan_row is not None:
        plan = PlanView(
            id=plan_row.id,
            version=plan_row.version,
            objective=plan_row.objective,
            plan_hash=plan_row.plan_hash,
            steps=[
                PlanStepView(
                    id=step.id,
                    step_index=step.step_index,
                    capability_id=step.capability_id,
                    status=step.status,
                    permission_key=step.permission_key,
                    risk=RiskLevel(step.risk),
                    depends_on=list(step.depends_on),
                    input_payload=dict(step.input_payload),
                    input_bindings=dict(step.input_bindings),
                    operation_hash=step.operation_hash,
                    verification_kind=step.verification_kind,
                )
                for step in steps_for_plan(session, plan_row.id)
            ],
        )
    error = None
    if task.error_code and task.error_message:
        error = TaskErrorView(code=task.error_code, message=task.error_message)
    return TaskView(
        id=task.id,
        status=TaskStatus(task.status),
        request_id=task.request_id,
        request=task.request_text,
        objective=task.objective,
        outcome_kind=OutcomeKind(task.outcome_kind) if task.outcome_kind else None,
        result=task.result_payload,
        error=error,
        plan=plan,
        approvals=[
            ApprovalView(
                id=approval.id,
                task_id=approval.task_id,
                plan_step_id=approval.plan_step_id,
                capability_id=approval.capability_id,
                status=ApprovalStatus(approval.status),
                operation_hash=approval.operation_hash,
                canonical_payload=dict(approval.canonical_payload),
                canonical_bindings=dict(approval.canonical_bindings),
                requested_by_principal_id=approval.requested_by_principal_id,
                decided_by_principal_id=approval.decided_by_principal_id,
                reason=approval.reason,
                expires_at=approval.expires_at,
                decided_at=approval.decided_at,
                created_at=approval.created_at,
            )
            for approval in approvals_for_task(session, task.id)
        ],
        executions=[
            ExecutionView(
                id=execution.id,
                plan_step_id=execution.plan_step_id,
                capability_id=execution.capability_id,
                attempt=execution.attempt,
                status=ExecutionStatus(execution.status),
                outcome_certainty=execution.outcome_certainty,
                operation_hash=execution.operation_hash,
                input_payload=dict(execution.input_payload),
                output_payload=dict(execution.output_payload) if execution.output_payload else None,
                evidence=dict(execution.evidence),
                error_code=execution.error_code,
                error_message=execution.error_message,
                started_at=execution.started_at,
                finished_at=execution.finished_at,
            )
            for execution in executions_for_task(session, task.id)
        ],
        verifications=[
            VerificationView(
                id=verification.id,
                execution_id=verification.execution_id,
                status=VerificationStatus(verification.status),
                checks=[CheckResult.model_validate(check) for check in verification.checks],
                summary=verification.summary,
                advisory_note=verification.advisory_note,
                advisory_suggestion=verification.advisory_suggestion,
                advisory_disagrees=verification.advisory_disagrees,
            )
            for verification in verifications_for_task(session, task.id)
        ],
        events=[
            TaskEventView(
                id=event.id,
                event_type=event.event_type,
                from_status=event.from_status,
                to_status=event.to_status,
                payload=dict(event.payload),
                created_at=event.created_at,
            )
            for event in events_for_task(session, task.id)
        ],
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def status_view(task: Task) -> TaskStatusView:
    """Return the smaller status payload."""
    error = None
    if task.error_code and task.error_message:
        error = TaskErrorView(code=task.error_code, message=task.error_message)
    return TaskStatusView(
        id=task.id,
        status=TaskStatus(task.status),
        outcome_kind=OutcomeKind(task.outcome_kind) if task.outcome_kind else None,
        updated_at=task.updated_at,
        error=error,
    )
