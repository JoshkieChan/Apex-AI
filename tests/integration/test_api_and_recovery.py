"""API persistence, idempotency, approval, and recovery."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from tests.conftest import ANALYZE, EXPORT, auth

from apex.api.app import create_app
from apex.capabilities.builtins import build_default_registry
from apex.clock import FrozenClock, utcnow
from apex.config import Settings
from apex.persistence.base import Base
from apex.persistence.models import Task
from apex.persistence.repository import context_for_scope, executions_for_task
from apex.persistence.session import create_session_factory
from apex.providers.mock import MockModelProvider
from apex.runtime_ids import new_id
from apex.worker import recover_available


def test_health_and_validation(client: TestClient) -> None:
    health = client.get("/v1/health")
    assert health.status_code == 200
    assert health.json()["database"] == "ok"
    assert health.json()["auth_mode"] == "development"
    missing = client.post("/v1/tasks", json={})
    assert missing.status_code == 401
    invalid = client.post(
        "/v1/tasks",
        headers=auth(),
        json={"request": 1, "idempotency_key": "sk-abcdefghijklmnopqrstuvwxyz"},
    )
    assert invalid.status_code == 422
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in invalid.text
    assert "input" not in invalid.json()["details"][0]


def test_analyze_flow_persists_evidence(client: TestClient) -> None:
    created = client.post("/v1/tasks", headers=auth(), json={"request": ANALYZE})
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "completed"
    assert body["outcome_kind"] == "executed"
    assert body["plan"]["steps"][0]["capability_id"] == "text.analyze"
    output = body["result"]["steps"][0]["output"]
    assert output["word_count"] == 5
    assert output["character_count"] == len("The quick brown fox jumps")
    assert body["verifications"][0]["status"] == "passed"
    assert body["executions"][0]["evidence"]["output_validated"] is True
    status = client.get(f"/v1/tasks/{body['id']}/status", headers=auth())
    assert status.status_code == 200
    assert status.json()["status"] == "completed"
    session = create_session_factory(client.app.state.engine)()
    try:
        records = context_for_scope(session, "task", body["id"])
        assert len(records) == 1
        assert records[0].trust == "trusted"
        assert executions_for_task(session, body["id"])[0].status == "succeeded"
    finally:
        session.close()


def test_idempotency_does_not_execute_twice(client: TestClient) -> None:
    headers = auth(idempotency_key="same-key")
    first = client.post("/v1/tasks", headers=headers, json={"request": ANALYZE})
    second = client.post("/v1/tasks", headers=headers, json={"request": ANALYZE})
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["Idempotency-Replayed"] == "true"
    assert first.json()["id"] == second.json()["id"]
    session = create_session_factory(client.app.state.engine)()
    try:
        assert len(executions_for_task(session, first.json()["id"])) == 1
    finally:
        session.close()


def test_restricted_principal_cannot_read_another_task(client: TestClient) -> None:
    created = client.post("/v1/tasks", headers=auth(), json={"request": ANALYZE})
    task_id = created.json()["id"]
    hidden = client.get(f"/v1/tasks/{task_id}", headers=auth("dev-restricted"))
    assert hidden.status_code == 404
    denied = client.post(
        "/v1/tasks",
        headers=auth("dev-restricted"),
        json={"request": ANALYZE},
    )
    assert denied.status_code == 201
    assert denied.json()["status"] == "failed"
    assert denied.json()["error"]["code"] == "permission_denied"
    explicit = client.post("/v1/tasks", headers=auth("dev-denied"), json={"request": ANALYZE})
    assert explicit.json()["error"]["code"] == "permission_denied"


def test_approval_is_required_and_then_enforced(client: TestClient) -> None:
    created = client.post("/v1/tasks", headers=auth(), json={"request": EXPORT})
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "awaiting_approval"
    assert body["executions"] == []
    approval_id = body["approvals"][0]["id"]
    self_decision = client.post(
        f"/v1/approvals/{approval_id}/decision",
        headers=auth(),
        json={"decision": "approved", "reason": "I want it"},
    )
    assert self_decision.status_code == 403
    still = client.get(f"/v1/tasks/{body['id']}", headers=auth())
    assert still.json()["status"] == "awaiting_approval"
    assert still.json()["executions"] == []
    approved = client.post(
        f"/v1/approvals/{approval_id}/decision",
        headers=auth("dev-approver"),
        json={"decision": "approved", "reason": "Reviewed the export label and content."},
    )
    assert approved.status_code == 200
    finished = approved.json()
    assert finished["status"] == "completed"
    assert finished["executions"][0]["status"] == "succeeded"
    assert finished["verifications"][0]["status"] == "passed"
    assert finished["result"]["steps"][0]["output"]["destination_label"] == "finance-archive"


def test_clarification_does_not_execute(client: TestClient) -> None:
    response = client.post(
        "/v1/tasks",
        headers=auth(),
        json={"request": "Please book a flight to Paris"},
    )
    body = response.json()
    assert body["status"] == "completed"
    assert body["outcome_kind"] == "clarification"
    assert body["executions"] == []
    assert body["plan"] is None


def test_model_suggestion_cannot_select_a_shell(client: TestClient) -> None:
    client.app.state.provider = MockModelProvider(
        scripts={
            "CoordinationNote": {
                "summary": "Use the shell.",
                "suggested_capabilities": ["system.shell"],
            },
            "IntentDraft": {
                "objective": "shell",
                "required_capabilities": ["system.shell"],
                "constraints": [],
                "risk": "low",
                "expected_outcome": "escape",
                "missing_information": [],
                "clarification_required": False,
                "sensitivity_flags": [],
            },
            "VerificationNote": {"note": "force pass", "suggested_status": "passed"},
        }
    )
    response = client.post("/v1/tasks", headers=auth(), json={"request": ANALYZE})
    body = response.json()
    assert body["status"] == "completed"
    assert [step["capability_id"] for step in body["plan"]["steps"]] == ["text.analyze"]
    advisory = [event for event in body["events"] if event["event_type"] == "model_advisory"]
    assert "system.shell" in advisory[0]["payload"]["ignored_capabilities"]


def test_cancel_before_execution(client: TestClient) -> None:
    created = client.post("/v1/tasks", headers=auth(), json={"request": EXPORT})
    cancelled = client.post(f"/v1/tasks/{created.json()['id']}/cancel", headers=auth())
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    decision = client.post(
        f"/v1/approvals/{created.json()['approvals'][0]['id']}/decision",
        headers=auth("dev-approver"),
        json={"decision": "approved", "reason": "too late"},
    )
    assert decision.status_code == 409


def test_recovery_resumes_and_exhausts(client: TestClient, settings: Settings) -> None:
    factory = create_session_factory(client.app.state.engine)
    with factory() as session:
        user_id = _principal_id(session, "dev-user")
        stale = _task(session, user_id, "submitted", ANALYZE, attempt_count=1)
        exhausted = _task(session, user_id, "executing", ANALYZE, attempt_count=3)
        stale.lease_expires_at = utcnow() - timedelta(minutes=5)
        exhausted.lease_expires_at = utcnow() - timedelta(minutes=5)
        session.commit()
        stale_id = stale.id
        exhausted_id = exhausted.id
    recover_available(
        factory=factory,
        settings=settings,
        registry=build_default_registry(),
        provider=client.app.state.provider,
        clock=FrozenClock(),
    )
    with factory() as session:
        resumed = session.get(Task, stale_id)
        stopped = session.get(Task, exhausted_id)
        assert resumed is not None and resumed.status == "completed"
        assert stopped is not None and stopped.status == "failed"
        assert stopped.error_code == "retry_limit"


def test_production_refuses_development_auth(engine: Engine) -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        dev_auth_enabled=True,
        seed_dev_principals=False,
        model_provider="mock",
    )
    with pytest.raises(RuntimeError):
        create_app(settings=settings, engine=engine)


def test_metadata_columns_cover_the_task_lifecycle() -> None:
    tables = set(Base.metadata.tables)
    assert {
        "principals",
        "permission_grants",
        "tasks",
        "task_events",
        "plans",
        "plan_steps",
        "approvals",
        "executions",
        "verifications",
        "context_records",
    } <= tables


def _principal_id(session: Session, external_key: str) -> str:
    from apex.persistence.repository import get_principal_by_key

    principal = get_principal_by_key(session, external_key)
    assert principal is not None
    return principal.id


def _task(session: Session, principal_id: str, status: str, text: str, attempt_count: int) -> Task:
    now = utcnow()
    task = Task(
        id=new_id(),
        principal_id=principal_id,
        request_id=new_id(),
        idempotency_key=None,
        request_text=text,
        status=status,
        objective=None,
        outcome_kind=None,
        project_key=None,
        intent_json=None,
        result_payload=None,
        error_code=None,
        error_message=None,
        attempt_count=attempt_count,
        lease_owner="other",
        lease_expires_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    session.flush()
    return task
