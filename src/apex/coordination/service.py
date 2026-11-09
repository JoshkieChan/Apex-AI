"""Coordinate logical agents. Capability work goes through ExecutionEngine."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apex.authority.engine import AuthorityEngine
from apex.capabilities.registry import CapabilityRegistry
from apex.clock import Clock
from apex.config import Settings
from apex.contracts.schemas import Decision, ExecutionStatus, PlanStepDraft, RiskLevel, TaskStatus
from apex.coordination.graph import (
    AgentRule,
    GraphNode,
    decompose,
    validate_graph,
)
from apex.coordination.leases import claim_goal, release_goal, repair_stale_subtasks
from apex.coordination.registry import agent_rows
from apex.errors import DomainError
from apex.execution.engine import ExecutionEngine
from apex.memory.store import MemoryStore
from apex.persistence.models import (
    AgentInvocation,
    Approval,
    Goal,
    GoalEvent,
    GoalSubtask,
    GoalVerification,
    LogicalAgent,
    Plan,
    PlanRevision,
    PlanStep,
    Principal,
    Task,
    TaskEvent,
)
from apex.planning.hashes import hashes_for_steps, plan_hash
from apex.runtime.transitions import transition
from apex.runtime_ids import new_id
from apex.verification.engine import verify_execution

_TERMINAL = frozenset({"completed", "partial", "failed", "cancelled", "uncertain"})
_OPEN = frozenset({"pending", "ready"})
_GOAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "submitted": frozenset({"planned", "failed", "cancelled"}),
    "planned": frozenset({"running", "awaiting_approval", "failed", "cancelled"}),
    "running": frozenset(
        {
            "awaiting_approval",
            "completed",
            "partial",
            "failed",
            "cancelled",
            "uncertain",
        }
    ),
    "awaiting_approval": frozenset({"running", "failed", "cancelled"}),
    "failed": frozenset({"running"}),
}


class CoordinationService:
    """Own a goal graph and schedule the smallest ready set."""

    def __init__(
        self,
        session: Session,
        settings: Settings,
        registry: CapabilityRegistry,
        clock: Clock,
    ) -> None:
        self._session = session
        self._settings = settings
        self._registry = registry
        self._clock = clock

    def submit(
        self,
        actor: Principal,
        objective: str,
        project_key: str | None,
        *,
        max_waves: int | None = None,
    ) -> dict[str, Any]:
        """Validate a deterministic graph, persist it, and run until it blocks."""
        self.ensure_agents()
        nodes, allow_partial = decompose(objective)
        validate_graph(
            nodes,
            agents=self._rules(),
            registry=self._registry,
            limit=self._settings.max_goal_subtasks,
        )
        now = self._clock.now()
        task = self._create_task(actor, objective, project_key, now)
        goal = Goal(
            id=new_id(),
            principal_id=actor.id,
            task_id=task.id,
            original_request=objective,
            objective=objective,
            project_key=project_key,
            status="planned",
            revision=1,
            autonomy="supervised",
            allow_partial=allow_partial,
            result_payload=None,
            error_code=None,
            created_at=now,
            updated_at=now,
            lease_owner=None,
            lease_expires_at=None,
            recovery_attempts=0,
        )
        self._session.add(goal)
        self._session.flush()
        self._store_graph(goal, task, nodes, reason="initial", trigger="submit")
        self._event(goal, "goal_submitted", actor, {"revision": 1})
        return self.advance(goal.id, actor, max_waves=self._waves(max_waves))

    def advance(
        self,
        goal_id: str,
        actor: Principal,
        *,
        max_waves: int | None = None,
    ) -> dict[str, Any]:
        """Run ready subtasks under a database lease. The worker uses the same claim."""
        goal = self._owned(goal_id, actor)
        if goal.status in _TERMINAL:
            return self.view(goal_id, actor)
        now = self._clock.now()
        claimed = claim_goal(
            self._session,
            goal.id,
            self._settings.instance_id,
            now,
            self._settings.lease_seconds,
            self._settings.max_goal_recoveries,
        )
        if not claimed:
            return self.view(goal_id, actor)
        # The claim is durable before any subtask runs. A crash after this commit
        # leaves the lease held until it expires.
        self._session.commit()
        try:
            repair_stale_subtasks(self._session, goal.id, self._registry, now)
            self._session.expire(goal)
            goal = self._owned(goal_id, actor)
            task = self._task(goal)
            waves = 0
            limit = self._waves(max_waves)
            while goal.status not in _TERMINAL:
                if limit is not None and waves >= limit:
                    self._set_status(goal, "running")
                    self._session.commit()
                    break
                ready = self._ready(goal)
                if not ready:
                    self._finish(goal, task)
                    self._session.commit()
                    break
                if task.status == TaskStatus.planned.value:
                    transition(
                        self._session,
                        task,
                        TaskStatus.executing,
                        event_type="goal_execution_started",
                        actor_id=actor.id,
                        payload={"goal_id": goal.id},
                        clock=self._clock,
                    )
                self._set_status(goal, "running")
                progressed = False
                for subtask in ready[: self._settings.max_goal_concurrency]:
                    progressed = self._run_one(goal, task, actor, subtask) or progressed
                    self._session.commit()
                waves += 1
                if not progressed:
                    self._finish(goal, task)
                    self._session.commit()
                    break
            self._hold_for_env("APEX_TEST_HOLD_LEASE")
        finally:
            release_goal(self._session, goal_id, self._settings.instance_id)
            self._session.commit()
        return self.view(goal_id, actor)

    def _hold_for_env(self, variable: str) -> None:
        """Test-only pause after a commit, so a process can be killed while the lease is held."""
        marker = os.environ.get(variable, "")
        if not marker or self._settings.environment != "test":
            return
        path = Path(marker)
        path.write_text("held", encoding="utf-8")
        deadline = time.monotonic() + 20
        while path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)

    def recover(self, goal_id: str) -> bool:
        """Resume one goal for its owning principal. A lost claim does no work."""
        goal = self._session.get(Goal, goal_id)
        if goal is None or goal.status not in {"planned", "running"}:
            return False
        actor = self._session.get(Principal, goal.principal_id)
        if actor is None:
            return False
        before = (goal.status, goal.revision, goal.updated_at)
        self.advance(goal_id, actor)
        self._session.refresh(goal)
        return (goal.status, goal.revision, goal.updated_at) != before

    def exhaust(self, goal_id: str) -> None:
        """Stop a goal that has used its recovery budget. It does not start work."""
        goal = self._session.get(Goal, goal_id)
        if goal is None or goal.status not in {"planned", "running"}:
            return
        self._set_status(goal, "failed")
        goal.error_code = "recovery_exhausted"
        self._fail_task(self._task(goal), "recovery_exhausted")
        self._event(goal, "recovery_exhausted", None, {"attempts": goal.recovery_attempts})

    def replan(
        self, goal_id: str, actor: Principal, *, max_waves: int | None = None
    ) -> dict[str, Any]:
        """Drop a failed agent step that had no side effect and keep completed work."""
        goal = self._owned(goal_id, actor)
        rows = self._subtasks(goal.id)
        if any(row.status == "uncertain" or row.error_code == "outcome_uncertain" for row in rows):
            raise DomainError(
                "outcome_uncertain",
                "An uncertain operation cannot be replanned or retried.",
            )
        failed = [
            row
            for row in rows
            if row.status == "failed"
            and row.error_code == "agent_failed"
            and row.execution_id is None
        ]
        if not failed:
            raise DomainError("replan_unavailable", "There is no failed agent step to revise.")
        if not goal.allow_partial:
            raise DomainError("partial_not_allowed", "This goal cannot drop a required step.")
        if goal.revision >= self._settings.max_plan_revisions:
            raise DomainError("revision_limit", "The goal has used its plan revisions.")
        completed = [
            row.subtask_key for row in rows if row.status == "completed" and row.capability_id
        ]
        for row in failed:
            row.status = "cancelled"
            row.updated_at = self._clock.now()
        for row in rows:
            if row.agent_key != "verifier" or row.status == "completed":
                continue
            row.depends_on = completed
            row.acceptance = {"kind": "word_counts", "sources": completed}
            row.status = "pending"
            row.error_code = None
            row.updated_at = self._clock.now()
        goal.revision += 1
        self._set_status(goal, "running")
        goal.error_code = None
        self._invalidate_pending_approvals(goal)
        self._session.add(
            PlanRevision(
                id=new_id(),
                goal_id=goal.id,
                revision=goal.revision,
                reason="Remove the failed agent step and verify completed work.",
                trigger="agent_failed",
                plan_json=self._snapshot(self._subtasks(goal.id)),
                created_at=self._clock.now(),
            )
        )
        self._event(goal, "plan_revised", actor, {"revision": goal.revision})
        return self.advance(goal.id, actor, max_waves=max_waves)

    def cancel(self, goal_id: str, actor: Principal) -> dict[str, Any]:
        """Stop new work. Cancellation does not prove a side effect was undone."""
        goal = self._owned(goal_id, actor)
        if goal.status in _TERMINAL:
            return self.view(goal_id, actor)
        now = self._clock.now()
        for row in self._subtasks(goal.id):
            if row.status in _OPEN:
                row.status = "cancelled"
                row.updated_at = now
        self._set_status(goal, "cancelled")
        task = self._task(goal)
        current = TaskStatus(task.status)
        if TaskStatus.cancelled in _legal(current):
            transition(
                self._session,
                task,
                TaskStatus.cancelled,
                event_type="goal_cancelled",
                actor_id=actor.id,
                payload={"goal_id": goal.id},
                clock=self._clock,
            )
        self._event(goal, "goal_cancelled", actor, {})
        return self.view(goal.id, actor)

    def set_agent_status(self, actor: Principal, agent_key: str, status: str) -> dict[str, Any]:
        """Enable or retire a role. This does not change any grant."""
        if status not in {"active", "disabled", "retired"}:
            raise DomainError("plan_invalid", "That agent status is not supported.")
        decision = AuthorityEngine(self._session).evaluate(
            actor.id, "capability.register", now=self._clock.now(), risk="low"
        )
        if decision != Decision.allow:
            raise DomainError("permission_denied", "The principal cannot manage agents.")
        self.ensure_agents()
        agent = self._session.scalar(
            select(LogicalAgent).where(LogicalAgent.agent_key == agent_key)
        )
        if agent is None:
            raise DomainError("not_found", "The agent was not found.")
        agent.status = status
        agent.updated_at = self._clock.now()
        return {"agent_key": agent.agent_key, "status": agent.status}

    def list_agents(self, actor: Principal) -> dict[str, Any]:
        """List role definitions. The caller is authenticated and receives no grants."""
        del actor
        self.ensure_agents()
        rows = self._session.scalars(select(LogicalAgent).order_by(LogicalAgent.agent_key)).all()
        return {
            "agents": [
                {
                    "agent_key": row.agent_key,
                    "agent_type": row.agent_type,
                    "display_name": row.display_name,
                    "status": row.status,
                    "autonomy": row.autonomy,
                }
                for row in rows
            ]
        }

    def view(self, goal_id: str, actor: Principal) -> dict[str, Any]:
        """Return the goal, its graph, and invocation identity. Memory content is omitted."""
        goal = self._owned(goal_id, actor)
        task = self._task(goal)
        subtasks = self._subtasks(goal.id)
        invocations = self._session.scalars(
            select(AgentInvocation)
            .where(AgentInvocation.goal_id == goal.id)
            .order_by(AgentInvocation.created_at)
        ).all()
        events = self._session.scalars(
            select(GoalEvent).where(GoalEvent.goal_id == goal.id).order_by(GoalEvent.created_at)
        ).all()
        return {
            "id": goal.id,
            "status": goal.status,
            "original_request": goal.original_request,
            "objective": goal.objective,
            "revision": goal.revision,
            "task_id": goal.task_id,
            "task_status": task.status,
            "allow_partial": goal.allow_partial,
            "error_code": goal.error_code,
            "result": goal.result_payload,
            "subtasks": [
                {
                    "key": row.subtask_key,
                    "agent_key": row.agent_key,
                    "status": row.status,
                    "capability_id": row.capability_id,
                    "depends_on": list(row.depends_on),
                    "error_code": row.error_code,
                    "attempt_count": row.attempt_count,
                    "execution_id": row.execution_id,
                    "output": row.output_payload,
                }
                for row in subtasks
            ],
            "invocations": [
                {
                    "id": row.id,
                    "agent_key": row.agent_key,
                    "principal_subject": actor.external_key,
                    "attempt": row.attempt,
                    "status": row.status,
                    "memory_ids": list(_object(row.input_payload).get("memory_ids", [])),
                }
                for row in invocations
            ],
            "events": [row.event_type for row in events],
            "recovery_attempts": goal.recovery_attempts,
            "verifications": [
                {
                    "revision": row.revision,
                    "status": row.status,
                    "method": row.method,
                    "shared_model": row.shared_model,
                    "failure_reason": row.failure_reason,
                }
                for row in self._session.scalars(
                    select(GoalVerification)
                    .where(GoalVerification.goal_id == goal.id)
                    .order_by(GoalVerification.created_at)
                ).all()
            ],
        }

    def ensure_agents(self) -> None:
        """Insert built-in roles that are missing. Do not reactivate a disabled role."""
        now = self._clock.now()
        existing = set(self._session.scalars(select(LogicalAgent.agent_key)).all())
        for row in agent_rows(now):
            key = str(row["agent_key"])
            if key in existing:
                continue
            self._session.add(LogicalAgent(id=new_id(), **row))
        self._session.flush()

    def _run_one(self, goal: Goal, task: Task, actor: Principal, subtask: GoalSubtask) -> bool:
        if self._invocation_count(goal.id) >= self._settings.max_agent_invocations:
            subtask.status = "failed"
            subtask.error_code = "invocation_limit"
            subtask.updated_at = self._clock.now()
            return False
        agent = self._session.scalar(
            select(LogicalAgent).where(LogicalAgent.agent_key == subtask.agent_key)
        )
        if agent is None or agent.status != "active":
            subtask.status = "failed"
            subtask.error_code = "agent_unavailable"
            subtask.updated_at = self._clock.now()
            return True
        now = self._clock.now()
        subtask.status = "running"
        subtask.attempt_count += 1
        subtask.updated_at = now
        memory_ids = self._memory_ids(actor, goal, subtask)
        invocation = AgentInvocation(
            id=new_id(),
            goal_id=goal.id,
            subtask_id=subtask.id,
            agent_key=subtask.agent_key,
            principal_id=actor.id,
            attempt=subtask.attempt_count,
            status="running",
            input_payload={"subtask": subtask.subtask_key, "memory_ids": memory_ids},
            output_payload=None,
            error_code=None,
            created_at=now,
            finished_at=None,
        )
        self._session.add(invocation)
        self._session.flush()
        self._event(
            goal, "agent_started", actor, {"subtask": subtask.subtask_key}, subtask.agent_key
        )
        self._session.commit()
        self._hold_for_env("APEX_TEST_HOLD_BEFORE_EXECUTION")
        if _object(subtask.input_payload).get("fail") is True:
            self._close(invocation, "failed", None, "agent_failed")
            subtask.status = "failed"
            subtask.error_code = "agent_failed"
            return True
        if subtask.capability_id is None:
            return self._verify(goal, subtask, invocation)
        return self._execute(goal, task, actor, subtask, invocation)

    def _execute(
        self,
        goal: Goal,
        task: Task,
        actor: Principal,
        subtask: GoalSubtask,
        invocation: AgentInvocation,
    ) -> bool:
        spec = self._registry.get(subtask.capability_id or "")
        if spec is None or not spec.available:
            self._close(invocation, "failed", None, "plan_invalid")
            subtask.status = "failed"
            subtask.error_code = "plan_invalid"
            return True
        decision = AuthorityEngine(self._session).evaluate(
            actor.id,
            spec.permission_key,
            now=self._clock.now(),
            risk=spec.risk.value,
        )
        if decision == Decision.deny:
            self._close(invocation, "failed", None, "permission_denied")
            subtask.status = "failed"
            subtask.error_code = "permission_denied"
            self._event(goal, "authority_denied", actor, {"subtask": subtask.subtask_key})
            return True
        if decision == Decision.require_approval:
            self._close(invocation, "blocked", None, "approval_required")
            subtask.status = "awaiting_approval"
            subtask.error_code = "approval_required"
            self._event(goal, "approval_required", actor, {"subtask": subtask.subtask_key})
            return False
        step = self._session.get(PlanStep, subtask.plan_step_id or "")
        if step is None:
            self._close(invocation, "failed", None, "plan_invalid")
            subtask.status = "failed"
            subtask.error_code = "plan_invalid"
            return True
        engine = ExecutionEngine(
            self._session,
            self._registry,
            AuthorityEngine(self._session),
            self._clock,
        )
        try:
            execution = engine.execute(
                task_id=task.id,
                principal_id=actor.id,
                step=step,
                approval=None,
                prior_outputs={},
                operation_hash=self._recomputed_hash(step, actor.id),
            )
        except DomainError as exc:
            code = "outcome_uncertain" if exc.code == "outcome_uncertain" else exc.code
            status = "uncertain" if code == "outcome_uncertain" else "failed"
            self._close(invocation, status, None, code)
            subtask.status = status
            subtask.error_code = code
            return True
        if execution.status != ExecutionStatus.succeeded.value:
            code = execution.error_code or "execution_failed"
            status = (
                "uncertain" if execution.status == ExecutionStatus.uncertain.value else "failed"
            )
            self._close(invocation, status, execution.output_payload, code)
            subtask.status = status
            subtask.error_code = code
            subtask.execution_id = execution.id
            return True
        output = dict(execution.output_payload or {})
        self._close(invocation, "completed", output, None)
        subtask.status = "completed"
        subtask.output_payload = output
        subtask.execution_id = execution.id
        subtask.error_code = None
        step.status = "completed"
        self._event(goal, "agent_completed", actor, {"subtask": subtask.subtask_key})
        return True

    def _verify(self, goal: Goal, subtask: GoalSubtask, invocation: AgentInvocation) -> bool:
        by_key = {row.subtask_key: row for row in self._subtasks(goal.id)}
        sources = _object(subtask.acceptance).get("sources", [])
        checks: list[dict[str, Any]] = []
        status = "passed"
        reason: str | None = None
        if not isinstance(sources, list) or not sources:
            status = "inconclusive"
            reason = "missing_evidence"
            checks.append({"source": "", "passed": False, "reason": "missing_evidence"})
        for source_key in sources if isinstance(sources, list) else []:
            source = by_key.get(str(source_key))
            output = _object(source.output_payload) if source is not None else {}
            payload = _object(source.input_payload) if source is not None else {}
            if source is None or not output:
                checks.append(
                    {"source": str(source_key), "passed": False, "reason": "missing_evidence"}
                )
                status = "inconclusive"
                reason = reason or "missing_evidence"
                continue
            kind = "text.analyze"
            if source.capability_id:
                spec = self._registry.get(source.capability_id)
                if spec is not None:
                    kind = spec.verification_kind
            report = verify_execution(
                verification_kind=kind,
                execution_status="succeeded",
                resolved_input=payload,
                output=output,
            )
            item_passed = report.status.value == "passed"
            checks.append(
                {
                    "source": str(source_key),
                    "passed": item_passed,
                    "reason": report.status.value,
                }
            )
            if report.status.value == "inconclusive" and status != "failed":
                status = "inconclusive"
                reason = reason or "inconclusive"
            elif not item_passed:
                status = "failed"
                reason = "verification_failed"
        self._record_verification(goal, subtask, status, reason, checks)
        result = {
            "status": status,
            "method": "deterministic_recompute",
            "shared_model": False,
            "revision": goal.revision,
            "checks": checks,
        }
        if status != "passed":
            code = "verification_failed" if status == "failed" else "verification_inconclusive"
            self._close(invocation, "failed", result, code)
            subtask.status = "failed"
            subtask.error_code = code
            subtask.output_payload = result
            self._event(
                goal, "verification_failed", None, {"subtask": subtask.subtask_key}, "verifier"
            )
            return True
        self._close(invocation, "completed", result, None)
        subtask.status = "completed"
        subtask.output_payload = result
        subtask.error_code = None
        self._event(goal, "verification_passed", None, {"subtask": subtask.subtask_key}, "verifier")
        return True

    def _ready(self, goal: Goal) -> list[GoalSubtask]:
        rows = self._subtasks(goal.id)
        by_key = {row.subtask_key: row for row in rows}
        ready: list[GoalSubtask] = []
        for row in rows:
            if row.status != "pending":
                continue
            dependency_states = [by_key[key].status for key in row.depends_on if key in by_key]
            if any(state in {"failed", "cancelled", "blocked"} for state in dependency_states):
                row.status = "blocked"
                row.updated_at = self._clock.now()
                continue
            if any(state == "uncertain" for state in dependency_states):
                row.status = "uncertain"
                row.updated_at = self._clock.now()
                continue
            if all(state == "completed" for state in dependency_states):
                ready.append(row)
        return ready

    def _finish(self, goal: Goal, task: Task) -> None:
        rows = self._subtasks(goal.id)
        statuses = {row.status for row in rows}
        if "awaiting_approval" in statuses:
            self._move_goal(goal, task, "awaiting_approval", TaskStatus.awaiting_approval, None)
            return
        if "uncertain" in statuses:
            self._move_goal(goal, task, "uncertain", TaskStatus.uncertain, "outcome_uncertain")
            return
        if statuses and statuses <= {"completed"}:
            self._succeed(goal, task)
            return
        if (
            goal.allow_partial
            and "completed" in statuses
            and "cancelled" in statuses
            and statuses <= {"completed", "cancelled"}
        ):
            self._partial(goal, task)
            return
        if statuses & {"pending", "running", "ready"}:
            self._set_status(goal, "running")
            return
        code = next((row.error_code for row in rows if row.error_code), "goal_failed")
        self._set_status(goal, "failed")
        goal.error_code = code
        goal.result_payload = self._aggregate(rows)
        goal.updated_at = self._clock.now()
        retryable = code == "agent_failed" and goal.allow_partial
        if not retryable:
            self._fail_task(task, code)
        self._event(goal, "goal_failed", None, {"error_code": code})

    def _succeed(self, goal: Goal, task: Task) -> None:
        if not self._verification_current(goal):
            self._set_status(goal, "failed")
            goal.error_code = "verification_missing"
            goal.result_payload = self._aggregate(self._subtasks(goal.id))
            self._fail_task(task, "verification_missing")
            self._event(goal, "goal_failed", None, {"error_code": "verification_missing"})
            return
        self._set_status(goal, "completed")
        goal.error_code = None
        goal.result_payload = self._aggregate(self._subtasks(goal.id))
        goal.updated_at = self._clock.now()
        if task.status == TaskStatus.executing.value:
            transition(
                self._session,
                task,
                TaskStatus.verifying,
                event_type="goal_verifying",
                actor_id=goal.principal_id,
                payload={"goal_id": goal.id},
                clock=self._clock,
            )
        if task.status == TaskStatus.verifying.value:
            transition(
                self._session,
                task,
                TaskStatus.completed,
                event_type="goal_completed",
                actor_id=goal.principal_id,
                payload={"goal_id": goal.id},
                clock=self._clock,
                extra={"outcome_kind": "goal", "result_payload": goal.result_payload},
            )
        self._event(goal, "goal_completed", None, {})

    def _partial(self, goal: Goal, task: Task) -> None:
        self._set_status(goal, "partial")
        goal.error_code = "partial_completion"
        goal.result_payload = self._aggregate(self._subtasks(goal.id))
        goal.updated_at = self._clock.now()
        self._fail_task(task, "partial_completion")
        self._event(goal, "goal_partial", None, {})

    def _fail_task(self, task: Task, code: str) -> None:
        current = TaskStatus(task.status)
        if TaskStatus.failed not in _legal(current):
            return
        transition(
            self._session,
            task,
            TaskStatus.failed,
            event_type="goal_task_failed",
            actor_id=task.principal_id,
            payload={"error_code": code},
            clock=self._clock,
            extra={
                "error_code": code,
                "outcome_kind": "partial" if code == "partial_completion" else "failed",
            },
        )

    def _move_goal(
        self,
        goal: Goal,
        task: Task,
        goal_status: str,
        task_status: TaskStatus,
        error_code: str | None,
    ) -> None:
        self._set_status(goal, goal_status)
        goal.error_code = error_code
        current = TaskStatus(task.status)
        if task_status in _legal(current):
            transition(
                self._session,
                task,
                task_status,
                event_type=f"goal_{goal_status}",
                actor_id=goal.principal_id,
                payload={"goal_id": goal.id},
                clock=self._clock,
                extra={"error_code": error_code} if error_code else None,
            )
        self._event(goal, f"goal_{goal_status}", None, {})

    def _set_status(self, goal: Goal, status: str) -> None:
        if goal.status == status:
            goal.updated_at = self._clock.now()
            return
        allowed = _GOAL_TRANSITIONS.get(goal.status, frozenset())
        if status not in allowed:
            raise DomainError("illegal_transition", "The goal cannot move to that state.")
        goal.status = status
        goal.updated_at = self._clock.now()

    def _record_verification(
        self,
        goal: Goal,
        subtask: GoalSubtask,
        status: str,
        reason: str | None,
        checks: list[dict[str, Any]],
    ) -> None:
        self._session.add(
            GoalVerification(
                id=new_id(),
                goal_id=goal.id,
                subtask_id=subtask.id,
                revision=goal.revision,
                method="deterministic_recompute",
                status=status,
                evidence={"checks": checks, "revision": goal.revision},
                failure_reason=reason,
                verifier="verifier",
                shared_model=False,
                created_at=self._clock.now(),
            )
        )

    def _verification_current(self, goal: Goal) -> bool:
        row = self._session.scalar(
            select(GoalVerification.id).where(
                GoalVerification.goal_id == goal.id,
                GoalVerification.revision == goal.revision,
                GoalVerification.status == "passed",
            )
        )
        return row is not None

    def _invalidate_pending_approvals(self, goal: Goal) -> None:
        rows = self._session.scalars(
            select(Approval).where(Approval.task_id == goal.task_id, Approval.status == "pending")
        ).all()
        now = self._clock.now()
        for row in rows:
            row.status = "cancelled"
            row.decided_at = now
            row.reason = "plan_revision"

    def _store_graph(
        self, goal: Goal, task: Task, nodes: list[GraphNode], *, reason: str, trigger: str
    ) -> None:
        drafts: list[PlanStepDraft] = []
        indexes: dict[str, int] = {}
        for node in nodes:
            if node.capability_id is None:
                continue
            spec = self._registry.get(node.capability_id)
            if spec is None:
                raise DomainError(
                    "plan_invalid", "The plan names a capability that is not available."
                )
            indexes[node.key] = len(drafts)
            drafts.append(
                PlanStepDraft(
                    step_index=len(drafts),
                    capability_id=spec.id,
                    objective=node.description,
                    input_payload=dict(node.input_payload),
                    permission_key=spec.permission_key,
                    risk=spec.risk,
                    verification_kind=spec.verification_kind,
                    failure_policy={"kind": "stop"},
                    capability_version=spec.version,
                    schema_hash=spec.schema_hash,
                )
            )
        hashes = hashes_for_steps(drafts, principal_id=task.principal_id)
        now = self._clock.now()
        plan = Plan(
            id=new_id(),
            task_id=task.id,
            version=goal.revision,
            objective=goal.objective,
            plan_hash=plan_hash(hashes),
            created_at=now,
        )
        self._session.add(plan)
        self._session.flush()
        step_ids: dict[str, str] = {}
        for node in nodes:
            if node.key not in indexes:
                continue
            draft = drafts[indexes[node.key]]
            step = PlanStep(
                id=new_id(),
                plan_id=plan.id,
                step_index=draft.step_index,
                capability_id=draft.capability_id,
                objective=draft.objective,
                input_payload=draft.input_payload,
                input_bindings={},
                depends_on=[],
                permission_key=draft.permission_key,
                risk=draft.risk.value,
                verification_kind=draft.verification_kind,
                capability_version=draft.capability_version,
                schema_hash=draft.schema_hash,
                failure_policy=draft.failure_policy,
                operation_hash=hashes[draft.step_index],
                status="pending",
            )
            self._session.add(step)
            step_ids[node.key] = step.id
        self._session.flush()
        for node in nodes:
            spec = self._registry.get(node.capability_id) if node.capability_id else None
            self._session.add(
                GoalSubtask(
                    id=new_id(),
                    goal_id=goal.id,
                    subtask_key=node.key,
                    agent_key=node.agent_key,
                    description=node.description,
                    capability_id=node.capability_id,
                    permission_key=spec.permission_key if spec is not None else None,
                    input_payload=dict(node.input_payload),
                    depends_on=list(node.depends_on),
                    acceptance=dict(node.acceptance),
                    status="pending",
                    plan_step_id=step_ids.get(node.key),
                    execution_id=None,
                    output_payload=None,
                    error_code=None,
                    attempt_count=0,
                    created_at=now,
                    updated_at=now,
                )
            )
        self._session.add(
            PlanRevision(
                id=new_id(),
                goal_id=goal.id,
                revision=goal.revision,
                reason=reason,
                trigger=trigger,
                plan_json=self._snapshot_nodes(nodes),
                created_at=now,
            )
        )
        self._session.flush()

    def _create_task(
        self, actor: Principal, objective: str, project_key: str | None, now: Any
    ) -> Task:
        task = Task(
            id=new_id(),
            principal_id=actor.id,
            request_id=new_id(),
            idempotency_key=None,
            request_text=objective,
            status=TaskStatus.submitted.value,
            objective=None,
            outcome_kind=None,
            project_key=project_key,
            intent_json=None,
            result_payload=None,
            error_code=None,
            error_message=None,
            attempt_count=1,
            delegation_id=None,
            lease_owner=None,
            lease_expires_at=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(task)
        self._session.add(
            TaskEvent(
                id=new_id(),
                task_id=task.id,
                event_type="task_submitted",
                from_status=None,
                to_status=TaskStatus.submitted.value,
                actor_principal_id=actor.id,
                payload={"kind": "goal"},
                created_at=now,
            )
        )
        self._session.flush()
        transition(
            self._session,
            task,
            TaskStatus.interpreting,
            event_type="goal_interpreted",
            actor_id=actor.id,
            payload={},
            clock=self._clock,
        )
        transition(
            self._session,
            task,
            TaskStatus.planned,
            event_type="goal_planned",
            actor_id=actor.id,
            payload={},
            clock=self._clock,
            extra={"objective": objective, "intent_json": {"kind": "coordinated_goal"}},
        )
        return task

    def _memory_ids(self, actor: Principal, goal: Goal, subtask: GoalSubtask) -> list[str]:
        try:
            assembled = MemoryStore(self._session, self._clock, self._settings).assemble(
                actor,
                query=subtask.description,
                task_id=goal.task_id,
                project_key=goal.project_key,
                budget=None,
                delegation_id=None,
            )
        except DomainError:
            return []
        return list(assembled.included_ids)

    def _recomputed_hash(self, step: PlanStep, principal_id: str) -> str:
        draft = PlanStepDraft(
            step_index=step.step_index,
            capability_id=step.capability_id,
            objective=step.objective,
            input_payload=dict(step.input_payload),
            input_bindings=dict(step.input_bindings or {}),
            depends_on=[int(item) for item in step.depends_on],
            permission_key=step.permission_key,
            risk=RiskLevel(step.risk),
            verification_kind=step.verification_kind,
            failure_policy=dict(step.failure_policy),
            capability_version=step.capability_version,
            schema_hash=step.schema_hash,
        )
        return hashes_for_steps([draft], principal_id=principal_id)[step.step_index]

    def _rules(self) -> dict[str, AgentRule]:
        rows = self._session.scalars(select(LogicalAgent)).all()
        return {
            row.agent_key: AgentRule(
                agent_key=row.agent_key,
                agent_type=row.agent_type,
                status=row.status,
                allowed_permissions=frozenset(row.allowed_permissions),
            )
            for row in rows
        }

    def _owned(self, goal_id: str, actor: Principal) -> Goal:
        goal = self._session.get(Goal, goal_id)
        if goal is None or goal.principal_id != actor.id:
            raise DomainError("not_found", "The goal was not found.")
        return goal

    def _task(self, goal: Goal) -> Task:
        task = self._session.get(Task, goal.task_id)
        if task is None:
            raise DomainError("not_found", "The goal was not found.")
        return task

    def _subtasks(self, goal_id: str) -> list[GoalSubtask]:
        return list(
            self._session.scalars(
                select(GoalSubtask)
                .where(GoalSubtask.goal_id == goal_id)
                .order_by(GoalSubtask.subtask_key)
            ).all()
        )

    def _invocation_count(self, goal_id: str) -> int:
        value = self._session.scalar(
            select(func.count())
            .select_from(AgentInvocation)
            .where(AgentInvocation.goal_id == goal_id)
        )
        return int(value or 0)

    def _close(
        self,
        invocation: AgentInvocation,
        status: str,
        output: dict[str, Any] | None,
        error_code: str | None,
    ) -> None:
        invocation.status = status
        invocation.output_payload = output
        invocation.error_code = error_code
        invocation.finished_at = self._clock.now()

    def _event(
        self,
        goal: Goal,
        event_type: str,
        actor: Principal | None,
        payload: dict[str, Any],
        agent_key: str | None = None,
    ) -> None:
        self._session.add(
            GoalEvent(
                id=new_id(),
                goal_id=goal.id,
                event_type=event_type,
                actor_principal_id=actor.id if actor is not None else None,
                agent_key=agent_key,
                payload=payload,
                created_at=self._clock.now(),
            )
        )

    def _waves(self, requested: int | None) -> int | None:
        if requested is None or self._settings.environment != "test":
            return None
        return requested

    def _aggregate(self, rows: list[GoalSubtask]) -> dict[str, Any]:
        def keys(status: str) -> list[str]:
            return [row.subtask_key for row in rows if row.status == status]

        verifier = next((row.output_payload for row in rows if row.agent_key == "verifier"), None)
        return {
            "completed": keys("completed"),
            "failed": keys("failed"),
            "blocked": keys("blocked"),
            "cancelled": keys("cancelled"),
            "uncertain": keys("uncertain"),
            "verification": verifier,
        }

    def _snapshot(self, rows: list[GoalSubtask]) -> dict[str, Any]:
        return {
            "nodes": [
                {
                    "key": row.subtask_key,
                    "agent_key": row.agent_key,
                    "capability_id": row.capability_id,
                    "depends_on": list(row.depends_on),
                    "status": row.status,
                }
                for row in rows
            ]
        }

    def _snapshot_nodes(self, nodes: list[GraphNode]) -> dict[str, Any]:
        return {
            "nodes": [
                {
                    "key": node.key,
                    "agent_key": node.agent_key,
                    "capability_id": node.capability_id,
                    "depends_on": list(node.depends_on),
                }
                for node in nodes
            ]
        }


def _legal(status: TaskStatus) -> frozenset[TaskStatus]:
    from apex.contracts.schemas import LEGAL_TRANSITIONS

    return LEGAL_TRANSITIONS[status]


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}
