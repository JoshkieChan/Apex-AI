"""Coordinate interpretation, planning, authority, execution, and verification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apex.agents.roles import Coordinator, Planner, Specialist, Verifier
from apex.authority.approvals import ApprovalService
from apex.authority.engine import AuthorityEngine
from apex.authority.scope import enforce_resource_scope
from apex.capabilities.registry import CapabilityRegistry
from apex.clock import Clock, ensure_aware
from apex.config import Settings
from apex.contracts.schemas import (
    IDLE_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    ApprovalStatus,
    Decision,
    ExecutionStatus,
    OutcomeKind,
    TaskStatus,
    TaskView,
    VerificationStatus,
)
from apex.errors import AdvanceYield, DomainError
from apex.execution.engine import (
    ExecutionEngine,
    latest_execution_for_step,
    unresolved_external_outcome,
)
from apex.intent.engine import IntentEngine
from apex.logging_config import get_logger
from apex.memory.service import MemoryService
from apex.observability import traced
from apex.persistence.models import Approval, Delegation, Plan, PlanStep, Task, TaskEvent
from apex.persistence.repository import (
    approval_for_step_hash,
    find_task_by_idempotency,
    get_task,
    latest_plan,
    steps_for_plan,
    succeeded_execution,
    verification_for_execution,
)
from apex.planning.engine import PlanningEngine
from apex.planning.hashes import hashes_for_steps, plan_hash
from apex.providers.base import ModelProvider
from apex.runtime.transitions import affected_rows, transition
from apex.runtime.views import task_view
from apex.runtime_ids import new_id

logger = get_logger("runtime")

_MAX_PHASES = 8


@dataclass
class RuntimeContext:
    """Dependencies for one unit of work. The session is not shared across requests."""

    session: Session
    registry: CapabilityRegistry
    provider: ModelProvider | None
    settings: Settings
    clock: Clock


class Orchestrator:
    """Drive a task until it finishes, waits for approval, or fails."""

    def __init__(self, context: RuntimeContext) -> None:
        self.ctx = context
        self.authority = AuthorityEngine(context.session)
        self.approvals = ApprovalService(
            context.session,
            self.authority,
            context.settings.approval_ttl_hours,
        )
        self.intent_engine = IntentEngine(context.registry, context.provider)
        self.planner = Planner(PlanningEngine(context.registry))
        self.execution = ExecutionEngine(
            context.session,
            context.registry,
            self.authority,
            context.clock,
        )
        self.coordinator = Coordinator(context.provider, context.registry)
        self.specialist = Specialist(self.execution)
        self.verifier = Verifier(context.provider)
        self.memory = MemoryService(context.session, context.clock, context.settings)

    def submit(
        self,
        *,
        principal_id: str,
        request_text: str,
        request_id: str,
        idempotency_key: str | None,
        project_key: str | None,
        delegation_id: str | None = None,
    ) -> tuple[TaskView, bool]:
        """Create a task and run it until it blocks or finishes.

        Replaying the same idempotency key returns the existing task.
        """
        now = self.ctx.clock.now()
        if delegation_id:
            delegation = self.ctx.session.get(Delegation, delegation_id)
            if (
                delegation is None
                or delegation.delegate_principal_id != principal_id
                or delegation.revoked_at is not None
                or ensure_aware(delegation.expires_at) <= now
            ):
                raise DomainError("delegation_forbidden", "The delegation cannot be used.")
        if idempotency_key:
            existing = find_task_by_idempotency(self.ctx.session, principal_id, idempotency_key)
            if existing is not None:
                return task_view(self.ctx.session, existing), False
        task = Task(
            id=new_id(),
            principal_id=principal_id,
            request_id=request_id,
            idempotency_key=idempotency_key,
            request_text=request_text,
            status=TaskStatus.submitted.value,
            objective=None,
            outcome_kind=None,
            project_key=project_key,
            intent_json=None,
            result_payload=None,
            error_code=None,
            error_message=None,
            attempt_count=1,
            delegation_id=delegation_id,
            lease_owner=self.ctx.settings.instance_id,
            lease_expires_at=now + timedelta(seconds=self.ctx.settings.lease_seconds),
            created_at=now,
            updated_at=now,
        )
        self.ctx.session.add(task)
        self.ctx.session.add(
            TaskEvent(
                id=new_id(),
                task_id=task.id,
                event_type="task_submitted",
                from_status=None,
                to_status=TaskStatus.submitted.value,
                actor_principal_id=principal_id,
                payload={"request_id": request_id},
                created_at=now,
            )
        )
        try:
            self.ctx.session.commit()
        except IntegrityError:
            self.ctx.session.rollback()
            if idempotency_key is None:
                raise
            raced = find_task_by_idempotency(self.ctx.session, principal_id, idempotency_key)
            if raced is None:
                raise
            return task_view(self.ctx.session, raced), False
        with traced("apex.task.submit", task_id=task.id, principal_id=principal_id):
            self.advance(task.id)
        stored = self._require_task(task.id)
        return task_view(self.ctx.session, stored), True

    def advance(self, task_id: str) -> None:
        """Claim the task and move it as far as the current approvals allow."""
        task = get_task(self.ctx.session, task_id)
        if task is None or TaskStatus(task.status) in IDLE_TASK_STATUSES:
            return
        if not self._claim(task_id):
            return
        try:
            with traced("apex.task.advance", task_id=task_id):
                self._run(task_id)
        except AdvanceYield:
            return
        except DomainError as exc:
            if exc.code == "conflict":
                return
            self._fail(task_id, exc.code, exc.message)
        except Exception:
            logger.exception("task advance failed task_id=%s", task_id)
            self._fail(task_id, "internal_error", "The task could not be completed.")
        finally:
            self._release(task_id)

    def decide_approval(
        self,
        *,
        approver_id: str,
        approval_id: str,
        decision: str,
        reason: str,
    ) -> TaskView:
        """Record a decision and resume the originating task when the decision allows it."""
        approval = self.approvals.decide(
            approver_id=approver_id,
            approval_id=approval_id,
            decision=decision,
            reason=reason,
            now=self.ctx.clock.now(),
        )
        self.ctx.session.add(
            TaskEvent(
                id=new_id(),
                task_id=approval.task_id,
                event_type="approval_decided",
                from_status=None,
                to_status=None,
                actor_principal_id=approver_id,
                payload={
                    "approval_id": approval.id,
                    "decision": approval.status,
                    "capability_id": approval.capability_id,
                },
                created_at=self.ctx.clock.now(),
            )
        )
        self.ctx.session.commit()
        self.advance(approval.task_id)
        return task_view(self.ctx.session, self._require_task(approval.task_id))

    def cancel(self, *, principal_id: str, task_id: str) -> TaskView:
        """Cancel a task that has not started executing."""
        task = self._require_owned(task_id, principal_id)
        current = TaskStatus(task.status)
        if current in TERMINAL_TASK_STATUSES or current not in {
            TaskStatus.submitted,
            TaskStatus.interpreting,
            TaskStatus.planned,
            TaskStatus.awaiting_approval,
        }:
            raise DomainError("not_cancellable", "The task can no longer be cancelled.")
        self.approvals.cancel_pending_for_task(task.id, self.ctx.clock.now())
        transition(
            self.ctx.session,
            task,
            TaskStatus.cancelled,
            event_type="task_cancelled",
            actor_id=principal_id,
            payload={},
            clock=self.ctx.clock,
            extra={"outcome_kind": OutcomeKind.failed.value},
        )
        self.ctx.session.commit()
        return task_view(self.ctx.session, task)

    def fail_exhausted(self, task_id: str) -> None:
        """Mark a stuck task failed after the recovery limit."""
        self._fail(task_id, "retry_limit", "The task exhausted its recovery attempts.")

    def view_for_owner(self, task_id: str, principal_id: str) -> TaskView:
        return task_view(self.ctx.session, self._require_owned(task_id, principal_id))

    def _run(self, task_id: str) -> None:
        for _ in range(_MAX_PHASES):
            task = self._require_task(task_id)
            status = TaskStatus(task.status)
            if status in TERMINAL_TASK_STATUSES:
                return
            if status == TaskStatus.awaiting_approval:
                if not self._continue_after_approval(task):
                    return
                continue
            if status == TaskStatus.submitted:
                transition(
                    self.ctx.session,
                    task,
                    TaskStatus.interpreting,
                    event_type="interpretation_started",
                    actor_id=task.principal_id,
                    payload={},
                    clock=self.ctx.clock,
                )
                self.ctx.session.commit()
                continue
            if status == TaskStatus.interpreting:
                self._interpret(task)
                continue
            if status == TaskStatus.planned:
                self._authorize(task)
                continue
            if status == TaskStatus.executing:
                self._execute_all(task)
                continue
            if status == TaskStatus.verifying:
                self._finalize(task)
                continue
            raise DomainError("illegal_transition", "The task is in an unsupported state.")
        raise DomainError("internal_error", "The task could not be completed.")

    def _interpret(self, task: Task) -> None:
        if latest_plan(self.ctx.session, task.id) is not None:
            transition(
                self.ctx.session,
                task,
                TaskStatus.planned,
                event_type="plan_reused",
                actor_id=task.principal_id,
                payload={},
                clock=self.ctx.clock,
            )
            self.ctx.session.commit()
            return
        assembled = self.memory.assemble_for_task(task)
        intent = self.intent_engine.interpret(task.request_text, memory=assembled)
        summary, ignored = self.coordinator.advise(
            task.request_text,
            set(intent.required_capabilities),
            memory=assembled,
        )
        task.intent_json = intent.model_dump(mode="json")
        task.objective = intent.objective
        self.ctx.session.add(
            TaskEvent(
                id=new_id(),
                task_id=task.id,
                event_type="model_advisory",
                from_status=task.status,
                to_status=task.status,
                actor_principal_id=task.principal_id,
                payload={
                    "summary": summary,
                    "ignored_capabilities": ignored,
                    "memory_ids": assembled.included_ids if assembled is not None else [],
                },
                created_at=self.ctx.clock.now(),
            )
        )
        if intent.clarification_required:
            task.outcome_kind = OutcomeKind.clarification.value
            task.result_payload = {
                "outcome_kind": OutcomeKind.clarification.value,
                "missing_information": intent.missing_information,
            }
            transition(
                self.ctx.session,
                task,
                TaskStatus.completed,
                event_type="clarification_required",
                actor_id=task.principal_id,
                payload={"missing_information": intent.missing_information},
                clock=self.ctx.clock,
                extra={
                    "outcome_kind": OutcomeKind.clarification.value,
                    "result_payload": task.result_payload,
                    "objective": task.objective,
                    "intent_json": task.intent_json,
                },
            )
            self.ctx.session.commit()
            return
        draft = self.planner.create_plan(intent)
        self._persist_plan(task, draft)
        transition(
            self.ctx.session,
            task,
            TaskStatus.planned,
            event_type="plan_created",
            actor_id=task.principal_id,
            payload={"objective": draft.objective, "steps": len(draft.steps)},
            clock=self.ctx.clock,
            extra={"objective": task.objective, "intent_json": task.intent_json},
        )
        self.ctx.session.commit()

    def _persist_plan(self, task: Task, draft: Any) -> None:
        hashes = hashes_for_steps(
            draft.steps,
            principal_id=task.principal_id,
            delegation_id=task.delegation_id,
            resource_scope=self._resource_scope(task),
        )
        now = self.ctx.clock.now()
        plan = Plan(
            id=new_id(),
            task_id=task.id,
            version=1,
            objective=draft.objective,
            plan_hash=plan_hash(hashes),
            created_at=now,
        )
        self.ctx.session.add(plan)
        self.ctx.session.flush()
        for step in draft.steps:
            self.ctx.session.add(
                PlanStep(
                    id=new_id(),
                    plan_id=plan.id,
                    step_index=step.step_index,
                    capability_id=step.capability_id,
                    objective=step.objective,
                    input_payload=step.input_payload,
                    input_bindings=step.input_bindings,
                    depends_on=step.depends_on,
                    permission_key=step.permission_key,
                    risk=step.risk.value,
                    verification_kind=step.verification_kind,
                    capability_version=step.capability_version,
                    schema_hash=step.schema_hash,
                    failure_policy=step.failure_policy,
                    operation_hash=hashes[step.step_index],
                    status="pending",
                )
            )
        self.ctx.session.flush()

    def _authorize(self, task: Task) -> None:
        plan, steps = self._plan_and_steps(task.id)
        self._require_hashes(task, steps)
        if self._blocked(task, plan, steps):
            return
        transition(
            self.ctx.session,
            task,
            TaskStatus.executing,
            event_type="execution_started",
            actor_id=task.principal_id,
            payload={},
            clock=self.ctx.clock,
        )
        self.ctx.session.commit()

    def _continue_after_approval(self, task: Task) -> bool:
        plan, steps = self._plan_and_steps(task.id)
        self._require_hashes(task, steps)
        if self._blocked(task, plan, steps):
            return False
        transition(
            self.ctx.session,
            task,
            TaskStatus.executing,
            event_type="approval_satisfied",
            actor_id=task.principal_id,
            payload={},
            clock=self.ctx.clock,
        )
        self.ctx.session.commit()
        return True

    def _blocked(self, task: Task, plan: Plan, steps: list[PlanStep]) -> bool:
        """Return True when the task is waiting or a denial has been raised."""
        waiting = False
        for step in steps:
            if step.status == "completed":
                continue
            self._enforce_scope(task, step)
            decision = self.authority.evaluate(
                task.principal_id,
                step.permission_key,
                now=self.ctx.clock.now(),
                delegation_id=task.delegation_id,
                risk=step.risk,
            )
            if decision == Decision.deny:
                raise DomainError(
                    "permission_denied",
                    "The principal is not allowed to use this capability.",
                )
            if decision != Decision.require_approval:
                continue
            approval = approval_for_step_hash(self.ctx.session, step.id, step.operation_hash)
            if approval is None:
                self.approvals.ensure_pending(
                    task_id=task.id,
                    plan_id=plan.id,
                    step=step,
                    requested_by=task.principal_id,
                    now=self.ctx.clock.now(),
                )
                waiting = True
                continue
            if approval.status == ApprovalStatus.pending.value:
                if ensure_aware(approval.expires_at) <= self.ctx.clock.now():
                    approval.status = ApprovalStatus.expired.value
                    self.ctx.session.flush()
                    raise DomainError("approval_expired", "This approval has expired.")
                waiting = True
                continue
            if approval.status == ApprovalStatus.rejected.value:
                raise DomainError("approval_rejected", "The operation was rejected.")
            if approval.status != ApprovalStatus.approved.value:
                raise DomainError("approval_required", "This operation requires approval.")
        if waiting:
            if TaskStatus(task.status) != TaskStatus.awaiting_approval:
                transition(
                    self.ctx.session,
                    task,
                    TaskStatus.awaiting_approval,
                    event_type="approval_required",
                    actor_id=task.principal_id,
                    payload={},
                    clock=self.ctx.clock,
                )
            self.ctx.session.commit()
            return True
        return False

    def _execute_all(self, task: Task) -> None:
        _plan, steps = self._plan_and_steps(task.id)
        self._require_hashes(task, steps)
        outputs: dict[int, dict[str, Any]] = {}
        for step in steps:
            if step.status == "completed":
                existing = succeeded_execution(self.ctx.session, step.id)
                if existing is not None and isinstance(existing.output_payload, dict):
                    outputs[step.step_index] = existing.output_payload
                continue
            self._enforce_scope(task, step)
            decision = self.authority.evaluate(
                task.principal_id,
                step.permission_key,
                now=self.ctx.clock.now(),
                delegation_id=task.delegation_id,
                risk=step.risk,
            )
            approval = None
            if decision == Decision.deny:
                raise DomainError(
                    "permission_denied",
                    "The principal is not allowed to use this capability.",
                )
            if decision == Decision.require_approval:
                approval = approval_for_step_hash(self.ctx.session, step.id, step.operation_hash)
                if approval is None or approval.status != ApprovalStatus.approved.value:
                    plan = latest_plan(self.ctx.session, task.id)
                    if plan is None:
                        raise DomainError("invalid_plan", "The task has no plan.")
                    self._blocked(task, plan, steps)
                    return
            self.ctx.session.add(
                TaskEvent(
                    id=new_id(),
                    task_id=task.id,
                    event_type="specialist_delegated",
                    from_status=task.status,
                    to_status=task.status,
                    actor_principal_id=task.principal_id,
                    payload={"capability_id": step.capability_id, "step_index": step.step_index},
                    created_at=self.ctx.clock.now(),
                )
            )
            self.ctx.session.commit()
            with traced(
                "apex.execute",
                task_id=task.id,
                capability_id=step.capability_id,
                principal_id=task.principal_id,
            ):
                try:
                    execution = self.specialist.perform(
                        task_id=task.id,
                        principal_id=task.principal_id,
                        step=step,
                        approval=approval,
                        prior_outputs=outputs,
                        operation_hash=step.operation_hash,
                        delegation_id=task.delegation_id,
                    )
                except DomainError as exc:
                    persisted = latest_execution_for_step(self.ctx.session, step.id)
                    if unresolved_external_outcome(persisted):
                        raise DomainError(
                            "outcome_uncertain",
                            "The external outcome could not be confirmed.",
                        ) from exc
                    raise
            if execution.status == ExecutionStatus.uncertain.value:
                raise DomainError(
                    "outcome_uncertain",
                    "The external outcome could not be confirmed.",
                )
            report = self.verifier.review(
                verification_kind=step.verification_kind,
                execution_status=execution.status,
                resolved_input=dict(execution.input_payload),
                output=dict(execution.output_payload) if execution.output_payload else None,
            )
            self._persist_verification(task.id, execution.id, report)
            if report.status == VerificationStatus.inconclusive:
                raise DomainError(
                    "outcome_uncertain",
                    "Verification could not establish the external outcome.",
                )
            if report.status != VerificationStatus.passed:
                step.status = "failed"
                self.ctx.session.commit()
                raise DomainError("verification_failed", "Verification did not pass.")
            step.status = "completed"
            if isinstance(execution.output_payload, dict):
                outputs[step.step_index] = execution.output_payload
            self.ctx.session.commit()
        transition(
            self.ctx.session,
            task,
            TaskStatus.verifying,
            event_type="verification_started",
            actor_id=task.principal_id,
            payload={},
            clock=self.ctx.clock,
        )
        self.ctx.session.commit()

    def _persist_verification(self, task_id: str, execution_id: str, report: Any) -> None:
        if verification_for_execution(self.ctx.session, execution_id) is not None:
            return
        from apex.persistence.models import Verification

        self.ctx.session.add(
            Verification(
                id=new_id(),
                execution_id=execution_id,
                task_id=task_id,
                status=report.status.value,
                checks=[check.model_dump() for check in report.checks],
                summary=report.summary,
                advisory_note=report.advisory_note,
                advisory_suggestion=(
                    report.advisory_suggestion.value if report.advisory_suggestion else None
                ),
                advisory_disagrees=report.advisory_disagrees,
                created_at=self.ctx.clock.now(),
            )
        )
        self.ctx.session.flush()

    def _finalize(self, task: Task) -> None:
        _plan, steps = self._plan_and_steps(task.id)
        step_results: list[dict[str, Any]] = []
        for step in steps:
            execution = succeeded_execution(self.ctx.session, step.id)
            if execution is None:
                raise DomainError("verification_failed", "Verification did not pass.")
            verification = verification_for_execution(self.ctx.session, execution.id)
            if verification is None or verification.status != VerificationStatus.passed.value:
                raise DomainError("verification_failed", "Verification did not pass.")
            step_results.append(
                {
                    "capability_id": step.capability_id,
                    "output": execution.output_payload,
                    "verification": verification.status,
                    "evidence": execution.evidence,
                }
            )
        result = {
            "outcome_kind": OutcomeKind.executed.value,
            "objective": task.objective,
            "steps": step_results,
        }
        self.memory.record_verified_task(task)
        self.memory.remember_verified_outcome(
            task,
            {
                "task_id": task.id,
                "capabilities": [item["capability_id"] for item in step_results],
                "verification": VerificationStatus.passed.value,
                "outputs": [item["output"] for item in step_results],
            },
        )
        transition(
            self.ctx.session,
            task,
            TaskStatus.completed,
            event_type="task_completed",
            actor_id=task.principal_id,
            payload={"steps": len(step_results)},
            clock=self.ctx.clock,
            extra={"outcome_kind": OutcomeKind.executed.value, "result_payload": result},
        )
        self.ctx.session.commit()

    def _resource_scope(self, task: Task) -> str:
        if not task.delegation_id:
            return ""
        delegation = self.ctx.session.get(Delegation, task.delegation_id)
        if delegation is None:
            return ""
        return delegation.resource_scope

    def _enforce_scope(self, task: Task, step: PlanStep) -> None:
        """Compare the planned payload with the delegation. Direct calls are unchanged."""
        if not task.delegation_id:
            return
        delegation = self.ctx.session.get(Delegation, task.delegation_id)
        now = self.ctx.clock.now()
        if (
            delegation is None
            or delegation.delegate_principal_id != task.principal_id
            or delegation.revoked_at is not None
            or ensure_aware(delegation.expires_at) <= now
        ):
            raise DomainError(
                "permission_denied",
                "The principal is not allowed to use this capability.",
            )
        spec = self.ctx.registry.get(step.capability_id)
        fields = spec.resource_fields if spec is not None else ()
        enforce_resource_scope(
            delegation.resource_scope,
            dict(step.input_payload or {}),
            fields,
        )

    def _require_hashes(self, task: Task, steps: list[PlanStep]) -> None:
        current = hashes_for_steps(
            steps,
            principal_id=task.principal_id,
            delegation_id=task.delegation_id,
            resource_scope=self._resource_scope(task),
        )
        for step in steps:
            if current[step.step_index] != step.operation_hash:
                raise DomainError(
                    "approval_mismatch",
                    "The operation no longer matches the recorded plan.",
                )

    def _plan_and_steps(self, task_id: str) -> tuple[Plan, list[PlanStep]]:
        plan = latest_plan(self.ctx.session, task_id)
        if plan is None:
            raise DomainError("invalid_plan", "The task has no plan.")
        return plan, steps_for_plan(self.ctx.session, plan.id)

    def _fail(self, task_id: str, code: str, message: str) -> None:
        self.ctx.session.rollback()
        task = get_task(self.ctx.session, task_id)
        if task is None or TaskStatus(task.status) in IDLE_TASK_STATUSES:
            return
        outcome = _outcome_for(code)
        target = TaskStatus.uncertain if code == "outcome_uncertain" else TaskStatus.failed
        event_type = "outcome_uncertain" if code == "outcome_uncertain" else "task_failed"
        try:
            transition(
                self.ctx.session,
                task,
                target,
                event_type=event_type,
                actor_id=task.principal_id,
                payload={"error_code": code},
                clock=self.ctx.clock,
                extra={
                    "outcome_kind": outcome,
                    "error_code": code,
                    "error_message": message,
                },
            )
            self.ctx.session.commit()
        except DomainError:
            self.ctx.session.rollback()

    def _claim(self, task_id: str) -> bool:
        now = self.ctx.clock.now()
        expiry = now + timedelta(seconds=self.ctx.settings.lease_seconds)
        owner = self.ctx.settings.instance_id
        result = self.ctx.session.execute(
            update(Task)
            .where(Task.id == task_id)
            .where(Task.status.notin_(tuple(status.value for status in IDLE_TASK_STATUSES)))
            .where(
                or_(
                    Task.lease_expires_at.is_(None),
                    Task.lease_expires_at < now,
                    Task.lease_owner == owner,
                )
            )
            .values(lease_owner=owner, lease_expires_at=expiry, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        self.ctx.session.commit()
        return affected_rows(result) == 1

    def _release(self, task_id: str) -> None:
        self.ctx.session.execute(
            update(Task)
            .where(Task.id == task_id, Task.lease_owner == self.ctx.settings.instance_id)
            .values(lease_owner=None, lease_expires_at=None)
        )
        self.ctx.session.commit()

    def _require_task(self, task_id: str) -> Task:
        task = get_task(self.ctx.session, task_id)
        if task is None:
            raise DomainError("task_not_found", "Task not found.")
        return task

    def _require_owned(self, task_id: str, principal_id: str) -> Task:
        task = self._require_task(task_id)
        if task.principal_id != principal_id:
            raise DomainError("task_not_found", "Task not found.")
        return task


def _outcome_for(code: str) -> str:
    if code == "permission_denied":
        return OutcomeKind.denied.value
    if code in {"approval_rejected", "approval_expired"}:
        return OutcomeKind.rejected.value
    if code == "outcome_uncertain":
        return OutcomeKind.uncertain.value
    return OutcomeKind.failed.value


def ready_after_approval_ids(session: Session) -> list[str]:
    """Tasks whose approvals are decided and that are waiting to resume."""
    pending = select(Approval.task_id).where(Approval.status == ApprovalStatus.pending.value)
    rows = session.scalars(
        select(Task.id).where(
            Task.status == TaskStatus.awaiting_approval.value,
            Task.lease_expires_at.is_(None),
            Task.id.notin_(pending),
        )
    )
    return list(rows)
