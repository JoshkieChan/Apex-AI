"""Lease claims and approval consumption stay atomic at the recorded boundary."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from tests.conftest import ANALYZE, EXPORT, auth

from apex.clock import utcnow
from apex.config import Settings
from apex.persistence.models import Approval, Task
from apex.persistence.repository import get_grant, get_principal_by_key
from apex.persistence.session import create_session_factory
from apex.runtime.factory import build_orchestrator
from apex.worker import recover_available


def test_active_lease_is_not_claimed_by_another_worker(
    client: TestClient, settings: Settings
) -> None:
    created = client.post("/v1/tasks", headers=auth(), json={"request": EXPORT})
    task_id = created.json()["id"]
    factory = create_session_factory(client.app.state.engine)
    with factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
        task.status = "submitted"
        task.lease_owner = "other-worker"
        task.lease_expires_at = task.updated_at + timedelta(minutes=5)
        session.commit()
        other = Settings(
            _env_file=None,
            environment="test",
            dev_auth_enabled=True,
            model_provider="mock",
            instance_id="second-worker",
            lease_seconds=60,
            max_task_attempts=3,
        )
        claimed = build_orchestrator(
            session=session,
            settings=other,
            registry=client.app.state.registry,
            provider=client.app.state.provider,
            clock=client.app.state.clock,
        )._claim(task_id)
        assert claimed is False
        session.refresh(task)
        assert task.lease_owner == "other-worker"
        assert task.status == "submitted"
    recover_available(
        factory=factory,
        settings=other,
        registry=client.app.state.registry,
        provider=client.app.state.provider,
        clock=client.app.state.clock,
    )
    with factory() as session:
        stored = session.get(Task, task_id)
        assert stored is not None
        assert stored.status == "submitted"
        assert stored.lease_owner == "other-worker"


def test_expired_approval_and_grant_do_not_execute(client: TestClient) -> None:
    created = client.post("/v1/tasks", headers=auth(), json={"request": EXPORT})
    approval_id = created.json()["approvals"][0]["id"]
    factory = create_session_factory(client.app.state.engine)
    with factory() as session:
        approval = session.get(Approval, approval_id)
        assert approval is not None
        approval.expires_at = utcnow() - timedelta(minutes=1)
        session.commit()
    expired = client.post(
        f"/v1/approvals/{approval_id}/decision",
        headers=auth("dev-approver"),
        json={"decision": "approved", "reason": "too late"},
    )
    assert expired.status_code == 409
    assert expired.json()["error"] == "approval_expired"
    current = client.get(f"/v1/tasks/{created.json()['id']}", headers=auth())
    assert current.json()["executions"] == []

    with factory() as session:
        principal = get_principal_by_key(session, "dev-user")
        assert principal is not None
        grant = get_grant(session, principal.id, "text.analyze")
        assert grant is not None
        grant.expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    denied = client.post("/v1/tasks", headers=auth(), json={"request": ANALYZE})
    assert denied.status_code == 201
    assert denied.json()["status"] == "failed"
    assert denied.json()["error"]["code"] == "permission_denied"
    assert denied.json()["executions"] == []
