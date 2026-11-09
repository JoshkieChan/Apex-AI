"""Acceptance coverage for the authorized path and the approval gate."""

from __future__ import annotations

from fastapi.testclient import TestClient
from tests.conftest import ANALYZE, COMBINED, auth

from apex.persistence.repository import events_for_task, verifications_for_task
from apex.persistence.session import create_session_factory


def test_request_is_planned_executed_verified_and_persisted(client: TestClient) -> None:
    response = client.post(
        "/v1/tasks",
        headers=auth(idempotency_key="e2e-analyze"),
        json={"request": ANALYZE, "project_key": "demo"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "completed"
    assert body["outcome_kind"] == "executed"
    assert body["plan"]["steps"][0]["capability_id"] == "text.analyze"
    assert body["result"]["steps"][0]["verification"] == "passed"
    assert body["result"]["steps"][0]["output"]["word_count"] == 5
    assert body["result"]["steps"][0]["evidence"]["capability_id"] == "text.analyze"
    session = create_session_factory(client.app.state.engine)()
    try:
        assert len(events_for_task(session, body["id"])) >= 4
        assert verifications_for_task(session, body["id"])[0].status == "passed"
    finally:
        session.close()


def test_multistep_export_waits_for_approval(client: TestClient) -> None:
    created = client.post("/v1/tasks", headers=auth(), json={"request": COMBINED})
    waiting = created.json()
    assert waiting["status"] == "awaiting_approval"
    assert [step["capability_id"] for step in waiting["plan"]["steps"]] == [
        "text.analyze",
        "records.export",
    ]
    assert waiting["executions"] == []
    approval_id = waiting["approvals"][0]["id"]
    assert waiting["approvals"][0]["capability_id"] == "records.export"
    finished = client.post(
        f"/v1/approvals/{approval_id}/decision",
        headers=auth("dev-approver"),
        json={"decision": "approved", "reason": "The archive label and source text were reviewed."},
    )
    body = finished.json()
    assert finished.status_code == 200
    assert body["status"] == "completed"
    assert len(body["executions"]) == 2
    assert all(item["status"] == "passed" for item in body["verifications"])
    assert body["result"]["steps"][1]["output"]["destination_label"] == "team-archive"
