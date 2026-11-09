"""Security boundaries: mutated approvals, secrets, and unregistered tools."""

from __future__ import annotations

from fastapi.testclient import TestClient
from tests.conftest import EXPORT, auth

from apex.persistence.models import PlanStep
from apex.persistence.session import create_session_factory


def test_modified_operation_does_not_keep_its_approval(client: TestClient) -> None:
    created = client.post("/v1/tasks", headers=auth(), json={"request": EXPORT})
    body = created.json()
    approval = body["approvals"][0]
    session = create_session_factory(client.app.state.engine)()
    try:
        step = session.get(PlanStep, approval["plan_step_id"])
        assert step is not None
        step.input_payload = {"destination_label": "other-archive", "content": {"note": "tampered"}}
        step.operation_hash = "tampered-hash"
        session.commit()
    finally:
        session.close()
    decision = client.post(
        f"/v1/approvals/{approval['id']}/decision",
        headers=auth("dev-approver"),
        json={"decision": "approved", "reason": "stale approval"},
    )
    assert decision.status_code == 409
    assert decision.json()["error"] == "approval_mismatch"
    current = client.get(f"/v1/tasks/{body['id']}", headers=auth())
    assert current.json()["status"] == "awaiting_approval"
    assert current.json()["executions"] == []


def test_rejected_approval_does_not_execute(client: TestClient) -> None:
    created = client.post("/v1/tasks", headers=auth(), json={"request": EXPORT})
    approval_id = created.json()["approvals"][0]["id"]
    rejected = client.post(
        f"/v1/approvals/{approval_id}/decision",
        headers=auth("dev-approver"),
        json={"decision": "rejected", "reason": "The destination label is not approved."},
    )
    assert rejected.status_code == 200
    body = rejected.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "approval_rejected"
    assert body["executions"] == []


def test_unknown_principal_and_mismatched_idempotency_key(client: TestClient) -> None:
    missing = client.post(
        "/v1/tasks", json={"request": "Analyze the following text and report its word count: hi"}
    )
    assert missing.status_code == 401
    unknown = client.post(
        "/v1/tasks",
        headers=auth("not-a-principal"),
        json={"request": "Analyze the following text and report its word count: hi"},
    )
    assert unknown.status_code == 401
    mismatch = client.post(
        "/v1/tasks",
        headers=auth(idempotency_key="header-key"),
        json={
            "request": "Analyze the following text and report its word count: hi",
            "idempotency_key": "body-key",
        },
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["error"] == "idempotency_conflict"


def test_unconfigured_auth_rejects_callers(client: TestClient) -> None:
    client.app.state.settings.dev_auth_enabled = False
    response = client.post(
        "/v1/tasks",
        headers=auth(),
        json={"request": "Analyze the following text and report its word count: hi"},
    )
    assert response.status_code == 401
    assert response.json()["error"] == "authentication_not_configured"
    client.app.state.settings.dev_auth_enabled = True
