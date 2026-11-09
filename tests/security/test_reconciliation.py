"""Uncertain external work is reconciled from fixture state, not from the lost response."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.conftest import auth

from apex.mcp import fixture
from apex.mcp.fixture import read_record, set_drop_after_write
from apex.persistence.models import Execution, ReconciliationAttempt

UPDATE = "Update fixture record beta status to released"


def test_lost_update_is_reconciled_from_independent_state(
    client: TestClient, session: Session
) -> None:
    _register_update(client)
    set_drop_after_write(True)
    created = client.post("/v1/tasks", headers=auth(), json={"request": UPDATE})
    approval = created.json()["approvals"][0]["id"]
    uncertain = client.post(
        f"/v1/approvals/{approval}/decision",
        headers=auth("dev-approver"),
        json={"decision": "approved", "reason": "apply it"},
    )
    body = uncertain.json()
    assert body["status"] == "uncertain"
    execution_id = body["executions"][0]["id"]
    assert body["executions"][0]["outcome_certainty"] == "possibly_sent"
    assert read_record("beta")["status"] == "released"
    denied = client.post(
        f"/v1/executions/{execution_id}/reconcile",
        headers=auth("dev-restricted"),
    )
    assert denied.status_code == 403
    confirmed = client.post(
        f"/v1/executions/{execution_id}/reconcile",
        headers=auth(),
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["outcome_certainty"] == "confirmed_success"
    assert confirmed.json()["evidence"]["observed_status"] == "released"
    again = client.post(f"/v1/executions/{execution_id}/reconcile", headers=auth())
    assert again.status_code == 200
    assert again.json()["attempts"] == 1
    stored = session.get(Execution, execution_id)
    assert stored is not None
    assert stored.outcome_certainty == "confirmed_success"
    assert session.query(ReconciliationAttempt).count() == 1
    viewed = client.get(f"/v1/tasks/{body['id']}", headers=auth())
    assert viewed.json()["status"] == "completed"


def test_reconciliation_confirms_failure_and_can_stay_inconclusive(
    client: TestClient, session: Session
) -> None:
    _register_update(client)
    set_drop_after_write(True)
    created = client.post("/v1/tasks", headers=auth(), json={"request": UPDATE})
    approval = created.json()["approvals"][0]["id"]
    uncertain = client.post(
        f"/v1/approvals/{approval}/decision",
        headers=auth("dev-approver"),
        json={"decision": "approved", "reason": "apply it"},
    )
    execution_id = uncertain.json()["executions"][0]["id"]
    fixture._applied.clear()
    fixture._records["beta"]["status"] = "held"
    failed = client.post(f"/v1/executions/{execution_id}/reconcile", headers=auth())
    assert failed.status_code == 200
    assert failed.json()["outcome_certainty"] == "confirmed_failure"
    assert (
        client.get(f"/v1/tasks/{uncertain.json()['id']}", headers=auth()).json()["status"]
        == "failed"
    )

    set_drop_after_write(True)
    second = client.post(
        "/v1/tasks",
        headers=auth(),
        json={"request": "Update fixture record alpha status to closed"},
    )
    approval_id = second.json()["approvals"][0]["id"]
    lost = client.post(
        f"/v1/approvals/{approval_id}/decision",
        headers=auth("dev-approver"),
        json={"decision": "approved", "reason": "close alpha"},
    )
    lost_id = lost.json()["executions"][0]["id"]
    fixture._applied.clear()
    held = client.post(f"/v1/executions/{lost_id}/reconcile", headers=auth())
    assert held.json()["outcome_certainty"] == "inconclusive"
    assert (
        client.get(f"/v1/tasks/{lost.json()['id']}", headers=auth()).json()["status"] == "uncertain"
    )


def test_not_sent_and_active_lease_are_not_retried_blindly(
    client: TestClient, session: Session
) -> None:
    _register_update(client)
    set_drop_after_write(True)
    created = client.post("/v1/tasks", headers=auth(), json={"request": UPDATE})
    approval = created.json()["approvals"][0]["id"]
    uncertain = client.post(
        f"/v1/approvals/{approval}/decision",
        headers=auth("dev-approver"),
        json={"decision": "approved", "reason": "apply it"},
    )
    execution_id = uncertain.json()["executions"][0]["id"]
    row = session.get(Execution, execution_id)
    assert row is not None
    row.reconcile_owner = "other-worker"
    row.reconcile_lease_expires = datetime.now(UTC) + timedelta(minutes=5)
    session.commit()
    leased = client.post(f"/v1/executions/{execution_id}/reconcile", headers=auth())
    assert leased.status_code == 409
    assert leased.json()["error"] == "conflict"
    row.reconcile_lease_expires = datetime.now(UTC) - timedelta(minutes=1)
    row.outcome_certainty = "not_sent"
    session.commit()
    blocked = client.post(f"/v1/executions/{execution_id}/reconcile", headers=auth())
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "not_reconcilable"
    follow = client.get(f"/v1/tasks/{uncertain.json()['id']}", headers=auth())
    assert follow.json()["status"] == "uncertain"


def _register_update(client: TestClient) -> None:
    client.post(
        "/v1/mcp/servers",
        headers=auth("dev-registrar"),
        json={"server_key": "fixture", "display_name": "Fixture"},
    )
    discovered = client.post("/v1/mcp/servers/fixture/discover", headers=auth("dev-registrar"))
    tool = next(
        item for item in discovered.json()["tools"] if item["tool_name"] == "records_update"
    )
    client.post(
        f"/v1/mcp/tools/{tool['id']}/register",
        headers=auth("dev-registrar"),
        json={
            "risk": "high",
            "permission_key": "records.update",
            "verification_kind": "records.update",
        },
    )
