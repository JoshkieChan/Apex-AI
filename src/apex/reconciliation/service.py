"""Reconcile an uncertain external operation from independent state.

A model note is not evidence.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from apex.clock import Clock
from apex.config import Settings
from apex.contracts.schemas import OutcomeKind, TaskStatus
from apex.errors import DomainError
from apex.mcp.fixture import operation_landed, read_record
from apex.persistence.models import (
    Execution,
    McpServerRecord,
    McpToolRecord,
    Principal,
    ReconciliationAttempt,
    Task,
    TaskEvent,
)
from apex.persistence.repository import get_grant
from apex.runtime.transitions import affected_rows, transition
from apex.runtime_ids import new_id

_OPEN = frozenset({"possibly_sent", "inconclusive"})
_SETTLED = frozenset({"confirmed_success", "confirmed_failure"})
_MAX_ATTEMPTS = 3
_LEASE_SECONDS = 30
_MANUAL = "Inspect the remote system for this operation id. Do not retry the write."


def _unverified(observation: str, **extra: Any) -> tuple[str, dict[str, Any]]:
    """Missing independent evidence stays inconclusive and names the manual path."""
    evidence: dict[str, Any] = {
        "observation": observation,
        "missing": "independent_readback",
        "manual_reconciliation": _MANUAL,
    }
    evidence.update(extra)
    return "inconclusive", evidence


class ReconciliationService:
    """Claim one operation, read its external state, and persist the resulting certainty."""

    def __init__(self, session: Session, settings: Settings, clock: Clock) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock

    def status(self, execution_id: str, actor: Principal) -> dict[str, Any]:
        execution, _task = self._authorized(execution_id, actor)
        attempts = self._attempts(execution.id)
        return _view(execution, attempts)

    def reconcile(self, execution_id: str, actor: Principal) -> dict[str, Any]:
        execution, task = self._authorized(execution_id, actor)
        if execution.outcome_certainty in _SETTLED:
            return _view(execution, self._attempts(execution.id))
        if execution.outcome_certainty == "not_sent":
            raise DomainError(
                "not_reconcilable",
                "The operation was not sent and must not be reconciled as an external effect.",
            )
        if execution.outcome_certainty not in _OPEN:
            raise DomainError("not_reconcilable", "The operation cannot be reconciled.")
        if len(self._attempts(execution.id)) >= _MAX_ATTEMPTS:
            raise DomainError("reconciliation_exhausted", "Reconciliation attempts are exhausted.")
        if not self._claim(execution.id, actor.id):
            raise DomainError("conflict", "Another reconciliation attempt holds this operation.")
        before = execution.outcome_certainty
        try:
            after, evidence = self._inspect(execution)
        except Exception:
            after = "inconclusive"
            evidence = {"observation": "unavailable"}
        self._session.refresh(execution)
        execution.outcome_certainty = after
        execution.reconcile_owner = None
        execution.reconcile_lease_expires = None
        stored = dict(execution.evidence or {})
        stored["outcome_certainty"] = after
        stored["reconciliation"] = evidence
        execution.evidence = stored
        now = self._clock.now()
        self._session.add(
            ReconciliationAttempt(
                id=new_id(),
                execution_id=execution.id,
                actor_principal_id=actor.id,
                certainty_before=before,
                certainty_after=after,
                evidence=evidence,
                created_at=now,
            )
        )
        self._session.add(
            TaskEvent(
                id=new_id(),
                task_id=task.id,
                event_type="reconciliation_attempt",
                from_status=task.status,
                to_status=task.status,
                actor_principal_id=actor.id,
                payload={"execution_id": execution.id, "certainty": after},
                created_at=now,
            )
        )
        self._finish_task(task, after, evidence)
        self._session.commit()
        return _view(execution, self._attempts(execution.id))

    def _authorized(self, execution_id: str, actor: Principal) -> tuple[Execution, Task]:
        grant = get_grant(self._session, actor.id, "operations.reconcile")
        if grant is None or grant.effect != "allow" or grant.revoked_at is not None:
            raise DomainError("permission_denied", "The principal cannot reconcile operations.")
        execution = self._session.get(Execution, execution_id)
        if execution is None:
            raise DomainError("not_found", "The operation does not exist.")
        task = self._session.get(Task, execution.task_id)
        if task is None or task.principal_id != actor.id:
            raise DomainError("not_found", "The operation does not exist.")
        return execution, task

    def _claim(self, execution_id: str, actor_id: str) -> bool:
        now = self._clock.now()
        result = self._session.execute(
            update(Execution)
            .where(Execution.id == execution_id)
            .where(Execution.outcome_certainty.in_(tuple(_OPEN)))
            .where(
                or_(
                    Execution.reconcile_lease_expires.is_(None),
                    Execution.reconcile_lease_expires < now,
                )
            )
            .values(
                reconcile_owner=actor_id,
                reconcile_lease_expires=now + timedelta(seconds=_LEASE_SECONDS),
            )
            .execution_options(synchronize_session=False)
        )
        self._session.commit()
        return affected_rows(result) == 1

    def _inspect(self, execution: Execution) -> tuple[str, dict[str, Any]]:
        tool = self._session.scalar(
            select(McpToolRecord).where(McpToolRecord.capability_id == execution.capability_id)
        )
        if tool is None:
            return _unverified("no_independent_read")
        server = self._session.get(McpServerRecord, tool.server_id)
        if server is None or server.transport != "inprocess" or tool.tool_name != "records_update":
            linked = self._session.scalar(
                select(McpToolRecord).where(
                    McpToolRecord.reconciles_capability_id == execution.capability_id,
                    McpToolRecord.available.is_(True),
                )
            )
            if linked is not None:
                return _unverified(
                    "remote_read_not_independent",
                    read_capability_id=linked.capability_id,
                )
            return _unverified("no_independent_read")
        payload = dict(execution.input_payload)
        name = payload.get("name")
        requested = payload.get("status")
        operation_id = execution.external_operation_id
        if not isinstance(name, str) or not isinstance(requested, str):
            return _unverified("missing_resource")
        observed = read_record(name)
        applied = operation_landed(operation_id) if isinstance(operation_id, str) else None
        observed_status = observed["status"] if observed is not None else None
        evidence = {
            "name": name,
            "requested_status": requested,
            "observed_status": observed_status,
            "operation_recorded": applied is not None,
            "reader": server.server_key,
        }
        if (
            applied is not None
            and applied.get("name") == name
            and applied.get("status") == requested
        ):
            if observed_status == requested:
                return "confirmed_success", evidence
            return _unverified("observed_state_differs", **evidence)
        if applied is None and observed_status != requested:
            return "confirmed_failure", evidence
        return _unverified("outcome_not_confirmed", **evidence)

    def _finish_task(self, task: Task, certainty: str, evidence: dict[str, Any]) -> None:
        if TaskStatus(task.status) != TaskStatus.uncertain:
            return
        if certainty == "confirmed_success":
            transition(
                self._session,
                task,
                TaskStatus.completed,
                event_type="reconciliation_confirmed",
                actor_id=task.principal_id,
                payload={"certainty": certainty},
                clock=self._clock,
                extra={
                    "outcome_kind": OutcomeKind.executed.value,
                    "result_payload": {"reconciliation": evidence},
                    "error_code": None,
                    "error_message": None,
                },
            )
        elif certainty == "confirmed_failure":
            transition(
                self._session,
                task,
                TaskStatus.failed,
                event_type="reconciliation_confirmed",
                actor_id=task.principal_id,
                payload={"certainty": certainty},
                clock=self._clock,
                extra={
                    "outcome_kind": OutcomeKind.failed.value,
                    "error_code": "confirmed_failure",
                    "error_message": "Independent state shows the operation did not apply.",
                },
            )

    def _attempts(self, execution_id: str) -> list[ReconciliationAttempt]:
        rows = self._session.scalars(
            select(ReconciliationAttempt)
            .where(ReconciliationAttempt.execution_id == execution_id)
            .order_by(ReconciliationAttempt.created_at)
        )
        return list(rows)


def _view(execution: Execution, attempts: list[ReconciliationAttempt]) -> dict[str, Any]:
    latest = attempts[-1].evidence if attempts else {}
    return {
        "execution_id": execution.id,
        "outcome_certainty": execution.outcome_certainty,
        "attempts": len(attempts),
        "evidence": latest,
    }
