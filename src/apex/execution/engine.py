"""Execution boundary. Authority is evaluated here, not only in the planner."""

from __future__ import annotations

from datetime import UTC
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apex.authority.engine import AuthorityEngine
from apex.authority.scope import enforce_resource_scope
from apex.capabilities.registry import CapabilityRegistry
from apex.clock import SystemClock, ensure_aware
from apex.contracts.schemas import ApprovalStatus, Decision, ExecutionStatus
from apex.errors import AdvanceYield, DomainError
from apex.execution.limits import run_bounded
from apex.execution.resolve import resolve_bindings
from apex.logging_config import get_logger
from apex.operations.controls import execution_suspended
from apex.persistence.models import Approval, Delegation, Execution, PlanStep
from apex.persistence.repository import executions_for_step, succeeded_execution
from apex.runtime_ids import canonical_json, new_id

logger = get_logger("execution")


class ExecutionEngine:
    """Validate, authorize, run, and record one plan step."""

    def __init__(
        self,
        session: Session,
        registry: CapabilityRegistry,
        authority: AuthorityEngine,
        clock: SystemClock | Any,
    ) -> None:
        self._session = session
        self._registry = registry
        self._authority = authority
        self._clock = clock

    def execute(
        self,
        *,
        task_id: str,
        principal_id: str,
        step: PlanStep,
        approval: Approval | None,
        prior_outputs: dict[int, dict[str, Any]],
        operation_hash: str,
        delegation_id: str | None = None,
    ) -> Execution:
        if not task_id or not principal_id:
            raise DomainError("missing_attribution", "Execution requires a task and a principal.")
        spec = self._registry.get(step.capability_id)
        if spec is None or not spec.available:
            raise DomainError(
                "unregistered_capability",
                "The operation names a capability that is not registered.",
            )
        if operation_hash != step.operation_hash:
            raise DomainError(
                "approval_mismatch", "The operation hash does not match the plan step."
            )
        if step.schema_hash and spec.schema_hash and step.schema_hash != spec.schema_hash:
            raise DomainError(
                "schema_changed",
                "The capability schema changed after this operation was planned.",
            )
        if step.capability_version and step.capability_version != spec.version:
            raise DomainError(
                "schema_changed",
                "The capability version changed after this operation was planned.",
            )
        decision = self._authority.evaluate(
            principal_id,
            spec.permission_key,
            now=self._clock.now(),
            delegation_id=delegation_id,
            risk=spec.risk.value,
        )
        if decision == Decision.deny:
            logger.info(
                "permission denied task_id=%s capability_id=%s",
                task_id,
                spec.id,
            )
            raise DomainError(
                "permission_denied", "The principal is not allowed to use this capability."
            )
        if decision == Decision.require_approval:
            self._require_matching_approval(approval, step, operation_hash)
        elif approval is not None:
            raise DomainError(
                "invalid_approval", "This operation does not accept an approval record."
            )

        existing = succeeded_execution(self._session, step.id)
        if existing is not None:
            return existing
        self._expire_running(step, spec.timeout_seconds, spec.idempotent)
        prior = executions_for_step(self._session, step.id)
        if any(item.status == ExecutionStatus.uncertain.value for item in prior):
            raise DomainError(
                "outcome_uncertain",
                "An earlier attempt may have changed external state.",
            )
        if approval is not None and approval.consumed_at is not None:
            raise DomainError("approval_replayed", "This approval was already used.")
        if any(item.status == ExecutionStatus.running.value for item in prior):
            raise AdvanceYield()
        if execution_suspended(self._session):
            logger.info(
                "execution held task_id=%s capability_id=%s",
                task_id,
                spec.id,
            )
            raise DomainError(
                "execution_suspended",
                "New capability execution is suspended.",
                dispatched=False,
            )
        attempt = (max((item.attempt for item in prior), default=0)) + 1
        resolved = resolve_bindings(
            _object(step.input_payload),
            _object(step.input_bindings),
            prior_outputs,
        )
        encoded = canonical_json(resolved).encode("utf-8")
        if len(encoded) > spec.max_input_bytes:
            raise DomainError("invalid_input", "The operation input exceeds its size limit.")
        try:
            validated = spec.input_model.model_validate(resolved)
        except ValidationError as exc:
            raise DomainError(
                "invalid_input", "The operation input does not match its schema."
            ) from exc
        payload = validated.model_dump()
        if delegation_id:
            self._enforce_delegation_scope(
                principal_id, delegation_id, spec.resource_fields, payload
            )
        prior_sent = [item for item in prior if item.outcome_certainty != "not_sent"]
        if len(prior_sent) >= spec.max_attempts:
            raise DomainError("retry_limit", "The operation has exhausted its retry limit.")

        now = self._clock.now()
        external_operation_id = payload.get("operation_id")
        record = Execution(
            id=new_id(),
            task_id=task_id,
            plan_step_id=step.id,
            principal_id=principal_id,
            approval_id=approval.id if approval is not None else None,
            capability_id=spec.id,
            attempt=attempt,
            status=ExecutionStatus.running.value,
            operation_hash=operation_hash,
            outcome_certainty="not_sent",
            external_operation_id=external_operation_id
            if isinstance(external_operation_id, str)
            else None,
            capability_version=spec.version,
            schema_hash=spec.schema_hash,
            input_payload=payload,
            output_payload=None,
            certainty="definite",
            evidence={
                "adapter": spec.id,
                "capability_id": spec.id,
                "operation_hash": operation_hash,
                "attempt": attempt,
                "timeout_seconds": spec.timeout_seconds,
                "input_validated": True,
                "idempotent": spec.idempotent,
            },
            error_code=None,
            error_message=None,
            started_at=now,
            finished_at=None,
        )
        if approval is not None:
            approval.consumed_at = now
        self._session.add(record)
        try:
            self._session.commit()
        except IntegrityError as exc:
            self._session.rollback()
            winner = succeeded_execution(self._session, step.id)
            if winner is not None:
                return winner
            raise AdvanceYield() from exc

        record.outcome_certainty = "possibly_sent"
        self._session.commit()
        try:
            raw = run_bounded(spec.adapter, payload, spec.timeout_seconds)
            output = spec.output_model.model_validate(raw)
        except DomainError as exc:
            if exc.code == "outcome_uncertain":
                self._finish_uncertain(record, exc)
            else:
                self._finish_failure(record, exc, idempotent=spec.idempotent)
            raise
        record.status = ExecutionStatus.succeeded.value
        record.certainty = "definite"
        record.outcome_certainty = "confirmed_success"
        record.output_payload = output.model_dump()
        record.finished_at = self._clock.now()
        evidence = dict(record.evidence)
        evidence["output_validated"] = True
        record.evidence = evidence
        self._session.commit()
        return record

    def _enforce_delegation_scope(
        self,
        principal_id: str,
        delegation_id: str,
        resource_fields: tuple[str, ...],
        payload: dict[str, Any],
    ) -> None:
        delegation = self._session.get(Delegation, delegation_id)
        now = self._clock.now()
        if (
            delegation is None
            or delegation.delegate_principal_id != principal_id
            or delegation.revoked_at is not None
            or ensure_aware(delegation.expires_at) <= now
        ):
            raise DomainError(
                "permission_denied",
                "The principal is not allowed to use this capability.",
            )
        enforce_resource_scope(delegation.resource_scope, payload, resource_fields)

    def _require_matching_approval(
        self,
        approval: Approval | None,
        step: PlanStep,
        operation_hash: str,
    ) -> None:
        if approval is None or approval.status != ApprovalStatus.approved.value:
            raise DomainError("approval_required", "This operation requires approval.")
        if approval.operation_hash != operation_hash:
            raise DomainError("approval_mismatch", "The approval does not match this operation.")
        if approval.plan_step_id != step.id or approval.capability_id != step.capability_id:
            raise DomainError("approval_mismatch", "The approval does not match this operation.")
        if _object(approval.canonical_payload) != _object(step.input_payload):
            raise DomainError(
                "approval_mismatch", "The approved input does not match this operation."
            )
        if _object(approval.canonical_bindings) != _object(step.input_bindings):
            raise DomainError(
                "approval_mismatch", "The approved input does not match this operation."
            )
        if _aware(approval.expires_at) <= self._clock.now():
            raise DomainError("approval_expired", "This approval has expired.")

    def _expire_running(self, step: PlanStep, timeout_seconds: float, idempotent: bool) -> None:
        from datetime import timedelta

        now = self._clock.now()
        changed = False
        for record in executions_for_step(self._session, step.id):
            if record.status != ExecutionStatus.running.value:
                continue
            started = _aware(record.started_at)
            if started + timedelta(seconds=timeout_seconds) < now:
                if record.outcome_certainty == "not_sent":
                    record.status = ExecutionStatus.failed.value
                    record.error_code = "not_sent"
                    record.error_message = "The operation was not sent."
                    record.finished_at = now
                    changed = True
                    continue
                if idempotent:
                    record.status = ExecutionStatus.timed_out.value
                    record.outcome_certainty = "inconclusive"
                    record.error_code = "timed_out"
                    record.error_message = "The operation exceeded its time limit."
                else:
                    record.status = ExecutionStatus.uncertain.value
                    record.certainty = "uncertain"
                    record.outcome_certainty = "possibly_sent"
                    record.error_code = "outcome_uncertain"
                    record.error_message = "The operation exceeded its time limit and may have run."
                record.finished_at = now
                changed = True
        if changed:
            self._session.commit()

    def _finish_uncertain(self, record: Execution, exc: DomainError) -> None:
        record.status = ExecutionStatus.uncertain.value
        record.certainty = "uncertain"
        record.error_code = exc.code
        record.error_message = exc.message
        record.finished_at = self._clock.now()
        record.outcome_certainty = "possibly_sent"
        evidence = dict(record.evidence or {})
        evidence["certainty"] = "uncertain"
        evidence["outcome_certainty"] = "possibly_sent"
        record.evidence = evidence
        self._session.commit()

    def _finish_failure(self, record: Execution, exc: DomainError, *, idempotent: bool) -> None:
        if _ambiguous_dispatch(exc, idempotent=idempotent):
            record.status = ExecutionStatus.uncertain.value
            record.certainty = "uncertain"
            record.outcome_certainty = "possibly_sent"
            record.error_code = exc.code
            record.error_message = exc.message
            record.finished_at = self._clock.now()
            evidence = dict(record.evidence or {})
            evidence["raised_code"] = exc.code
            evidence["outcome_certainty"] = "possibly_sent"
            evidence["transport_phase"] = "dispatched"
            record.evidence = evidence
            self._session.commit()
            return
        if exc.dispatched is False:
            record.status = ExecutionStatus.failed.value
            record.outcome_certainty = "not_sent"
            record.error_code = exc.code
            record.error_message = exc.message
            record.finished_at = self._clock.now()
            evidence = dict(record.evidence or {})
            evidence["raised_code"] = exc.code
            evidence["transport_phase"] = "pre_dispatch"
            record.evidence = evidence
            self._session.commit()
            return
        status = (
            ExecutionStatus.timed_out.value
            if exc.code == "timed_out"
            else ExecutionStatus.failed.value
        )
        record.status = status
        record.outcome_certainty = (
            "inconclusive" if exc.code == "timed_out" else "confirmed_failure"
        )
        record.error_code = exc.code
        record.error_message = exc.message
        record.finished_at = self._clock.now()
        self._session.commit()


def _ambiguous_dispatch(exc: DomainError, *, idempotent: bool) -> bool:
    """A non-idempotent call is uncertain only when it may already have been sent.

    A local adapter timeout has no transport phase, so it stays ambiguous.
    A remote failure that records dispatched=False happened before the tool
    request left the client and is a normal failure.
    """
    if idempotent or exc.dispatched is False:
        return False
    if exc.dispatched is True:
        return True
    return exc.code == "timed_out"


def unresolved_external_outcome(execution: Execution | None) -> bool:
    """True when the persisted row says an external side effect may have happened.

    A timeout code by itself is not enough. An idempotent timeout stays
    `inconclusive`, and a failure before the operation is sent has no row or
    stays `not_sent`.
    """
    if execution is None:
        return False
    if execution.status == ExecutionStatus.uncertain.value:
        return True
    return execution.outcome_certainty == "possibly_sent"


def latest_execution_for_step(session: Session, plan_step_id: str) -> Execution | None:
    """Return the newest persisted attempt for a plan step, if one exists."""
    rows = executions_for_step(session, plan_step_id)
    if not rows:
        return None
    return rows[-1]


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _aware(value: Any) -> Any:
    from datetime import datetime

    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
