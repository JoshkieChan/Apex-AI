"""Unit tests for classification, plan validation, and permission decisions."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session
from tests.conftest import ANALYZE, COMBINED, EXPORT

from apex.authority.approvals import ApprovalService
from apex.authority.engine import AuthorityEngine
from apex.capabilities.builtins import build_default_registry
from apex.clock import FrozenClock
from apex.contracts.schemas import Decision, Intent, PlanStepDraft, RiskLevel
from apex.errors import DomainError
from apex.intent.engine import IntentEngine
from apex.persistence.models import PermissionGrant, Plan, PlanStep, Principal
from apex.persistence.repository import get_grant
from apex.planning.engine import PlanningEngine
from apex.planning.validation import validate_plan
from apex.providers.mock import MockModelProvider
from apex.runtime_ids import new_id


def test_analyze_intent_selects_only_text_analyze() -> None:
    intent = IntentEngine(build_default_registry()).interpret(ANALYZE)
    assert intent.required_capabilities == ["text.analyze"]
    assert intent.inputs["text.analyze"]["text"] == "The quick brown fox jumps"
    assert intent.clarification_required is False
    assert intent.risk == RiskLevel.low


def test_export_and_combined_intents() -> None:
    registry = build_default_registry()
    engine = IntentEngine(registry)
    exported = engine.interpret(EXPORT)
    assert exported.required_capabilities == ["records.export"]
    assert exported.inputs["records.export"]["content"] == {"note": "quarterly"}
    combined = engine.interpret(COMBINED)
    assert combined.required_capabilities == ["text.analyze", "records.export"]
    assert combined.bindings["records.export"]["content"]["step_index"] == 0


def test_unknown_request_does_not_invent_a_capability() -> None:
    intent = IntentEngine(build_default_registry()).interpret("Please book a flight to Paris")
    assert intent.clarification_required is True
    assert intent.required_capabilities == []


def test_model_cannot_add_or_remove_capabilities() -> None:
    provider = MockModelProvider(
        scripts={
            "IntentDraft": {
                "objective": "Run a shell command.",
                "required_capabilities": ["system.shell"],
                "constraints": [],
                "risk": "low",
                "expected_outcome": "root",
                "missing_information": [],
                "clarification_required": False,
                "sensitivity_flags": [],
            }
        }
    )
    intent = IntentEngine(build_default_registry(), provider).interpret(ANALYZE)
    assert intent.required_capabilities == ["text.analyze"]
    assert "system.shell" in intent.dropped_model_capabilities
    assert intent.source == "deterministic+model"


def test_plan_rejects_unregistered_and_cyclic_dependencies() -> None:
    registry = build_default_registry()
    planner = PlanningEngine(registry)
    with pytest.raises(DomainError) as unregistered:
        planner.build(
            Intent(
                objective="nope",
                required_capabilities=["system.shell"],
                expected_outcome="nope",
            )
        )
    assert unregistered.value.code == "unregistered_capability"

    draft_steps = [
        PlanStepDraft(
            step_index=0,
            capability_id="text.analyze",
            objective="count",
            input_payload={"text": "one two"},
            depends_on=[1],
            permission_key="text.analyze",
            risk=RiskLevel.low,
            verification_kind="text.analyze",
            failure_policy={"max_attempts": 1, "on_failure": "fail_task"},
        )
    ]
    from apex.contracts.schemas import PlanDraft

    with pytest.raises(DomainError) as invalid:
        validate_plan(PlanDraft(objective="bad", steps=draft_steps), registry)
    assert invalid.value.code == "invalid_plan"


def test_authority_defaults_to_deny_and_honors_explicit_deny(session: Session) -> None:
    engine = AuthorityEngine(session)
    user = _principal(session, "unit-user")
    assert engine.evaluate(user.id, "text.analyze") == Decision.deny
    session.add(
        PermissionGrant(
            id=new_id(),
            principal_id=user.id,
            permission_key="text.analyze",
            effect="deny",
            created_at=FrozenClock().now(),
        )
    )
    session.commit()
    assert engine.evaluate(user.id, "text.analyze") == Decision.deny
    grant = get_grant(session, user.id, "text.analyze")
    assert grant is not None
    grant.effect = "allow"
    session.commit()
    assert engine.evaluate(user.id, "text.analyze") == Decision.allow


def test_requester_cannot_approve_and_mutation_does_not_match(session: Session) -> None:
    clock = FrozenClock()
    owner = _principal(session, "owner")
    approver = _principal(session, "approver")
    session.add(
        PermissionGrant(
            id=new_id(),
            principal_id=approver.id,
            permission_key="authority.approve",
            effect="allow",
            created_at=clock.now(),
        )
    )
    plan, step = _plan_step(session, owner.id, clock)
    service = ApprovalService(session, AuthorityEngine(session), ttl_hours=24)
    approval = service.ensure_pending(
        task_id=plan.task_id,
        plan_id=plan.id,
        step=step,
        requested_by=owner.id,
        now=clock.now(),
    )
    session.commit()
    with pytest.raises(DomainError) as self_approval:
        service.decide(
            approver_id=owner.id,
            approval_id=approval.id,
            decision="approved",
            reason="myself",
            now=clock.now(),
        )
    assert self_approval.value.code == "separation_of_duties"
    step.input_payload = {"text": "changed"}
    session.commit()
    with pytest.raises(DomainError) as mismatch:
        service.decide(
            approver_id=approver.id,
            approval_id=approval.id,
            decision="approved",
            reason="review",
            now=clock.now(),
        )
    assert mismatch.value.code == "approval_mismatch"
    clock.advance(timedelta(hours=48))
    step.input_payload = {"text": "one two"}
    session.commit()
    with pytest.raises(DomainError) as expired:
        service.decide(
            approver_id=approver.id,
            approval_id=approval.id,
            decision="approved",
            reason="late",
            now=clock.now(),
        )
    assert expired.value.code == "approval_expired"


def _principal(session: Session, key: str) -> Principal:
    from apex.clock import utcnow

    principal = Principal(
        id=new_id(),
        external_key=key,
        display_name=key,
        kind="user",
        created_at=utcnow(),
    )
    session.add(principal)
    session.flush()
    return principal


def _plan_step(session: Session, principal_id: str, clock: FrozenClock) -> tuple[Plan, PlanStep]:
    from apex.persistence.models import Task

    task = Task(
        id=new_id(),
        principal_id=principal_id,
        request_id="req",
        idempotency_key=None,
        request_text="Analyze the following text and report its word count: one two",
        status="awaiting_approval",
        objective="count",
        outcome_kind=None,
        project_key=None,
        intent_json=None,
        result_payload=None,
        error_code=None,
        error_message=None,
        attempt_count=1,
        lease_owner=None,
        lease_expires_at=None,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    session.add(task)
    session.flush()
    plan = Plan(
        id=new_id(),
        task_id=task.id,
        version=1,
        objective="count",
        plan_hash="abc",
        created_at=clock.now(),
    )
    session.add(plan)
    session.flush()
    step = PlanStep(
        id=new_id(),
        plan_id=plan.id,
        step_index=0,
        capability_id="text.analyze",
        objective="count",
        input_payload={"text": "one two"},
        input_bindings={},
        depends_on=[],
        permission_key="text.analyze",
        risk="low",
        verification_kind="text.analyze",
        failure_policy={"max_attempts": 1, "on_failure": "fail_task"},
        operation_hash="hash-1",
        status="pending",
    )
    session.add(step)
    session.flush()
    return plan, step
