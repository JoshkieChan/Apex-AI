"""Local MCP fixture through registration, authority, approval, and verification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from tests.conftest import auth

from apex.mcp.fixture import read_record, set_drop_after_write

LOOKUP = "Look up fixture record alpha"
UPDATE = "Update fixture record alpha status to closed"


def test_low_risk_lookup_is_registered_then_verified(client: TestClient) -> None:
    early = client.post("/v1/tasks", headers=auth(), json={"request": LOOKUP})
    assert early.status_code == 201
    assert early.json()["outcome_kind"] == "clarification"
    assert early.json()["executions"] == []

    denied = client.post(
        "/v1/mcp/servers",
        headers=auth(),
        json={"server_key": "fixture", "display_name": "Fixture"},
    )
    assert denied.status_code == 403

    tools = _register_fixture(client)
    assert all(tool["available"] is False for tool in tools)
    lookup = _tool(tools, "records_lookup")
    registered = client.post(
        f"/v1/mcp/tools/{lookup['id']}/register",
        headers=auth("dev-registrar"),
        json={
            "risk": "low",
            "permission_key": "records.lookup",
            "verification_kind": "records.lookup",
        },
    )
    assert registered.status_code == 200
    assert registered.json()["available"] is True
    assert registered.json()["risk"] == "low"

    completed = client.post("/v1/tasks", headers=auth(), json={"request": LOOKUP})
    assert completed.status_code == 201
    body = completed.json()
    assert body["status"] == "completed"
    assert body["outcome_kind"] == "executed"
    assert body["plan"]["steps"][0]["capability_id"] == "mcp.fixture.records_lookup"
    output = body["result"]["steps"][0]["output"]
    assert output["found"] is True
    assert output["record"]["status"] == "open"
    assert body["verifications"][0]["status"] == "passed"
    assert read_record("alpha") == {"name": "alpha", "status": "open"}

    hidden = client.get(f"/v1/tasks/{body['id']}", headers=auth("dev-restricted"))
    assert hidden.status_code == 404


def test_high_risk_update_requires_another_principal(client: TestClient) -> None:
    tools = _register_fixture(client)
    updated = _tool(tools, "records_update")
    client.post(
        f"/v1/mcp/tools/{updated['id']}/register",
        headers=auth("dev-registrar"),
        json={
            "risk": "high",
            "permission_key": "records.update",
            "verification_kind": "records.update",
        },
    )
    created = client.post("/v1/tasks", headers=auth(), json={"request": UPDATE})
    assert created.status_code == 201
    waiting = created.json()
    assert waiting["status"] == "awaiting_approval"
    assert waiting["executions"] == []
    assert read_record("alpha")["status"] == "open"
    approval_id = waiting["approvals"][0]["id"]

    own = client.post(
        f"/v1/approvals/{approval_id}/decision",
        headers=auth(),
        json={"decision": "approved", "reason": "I requested it."},
    )
    assert own.status_code == 403
    assert own.json()["error"] == "separation_of_duties"

    approved = client.post(
        f"/v1/approvals/{approval_id}/decision",
        headers=auth("dev-approver"),
        json={"decision": "approved", "reason": "The status change matches the request."},
    )
    assert approved.status_code == 200
    body = approved.json()
    assert body["status"] == "completed"
    assert body["verifications"][0]["status"] == "passed"
    assert read_record("alpha") == {"name": "alpha", "status": "closed"}
    assert len(body["executions"]) == 1

    replay = client.post(
        f"/v1/approvals/{approval_id}/decision",
        headers=auth("dev-approver"),
        json={"decision": "approved", "reason": "again"},
    )
    assert replay.status_code == 409
    assert replay.json()["error"] == "approval_already_decided"


def test_security_failures_block_execution(client: TestClient) -> None:
    tools = _register_fixture(client)
    updated = _tool(tools, "records_update")
    client.post(
        f"/v1/mcp/tools/{updated['id']}/register",
        headers=auth("dev-registrar"),
        json={
            "risk": "high",
            "permission_key": "records.update",
            "verification_kind": "records.update",
        },
    )
    created = client.post("/v1/tasks", headers=auth(), json={"request": UPDATE})
    approval = created.json()["approvals"][0]
    revoked = client.post(
        "/v1/grants/revoke",
        headers=auth("dev-approver"),
        json={"subject": "dev-user", "permission_key": "records.update"},
    )
    assert revoked.status_code == 200
    decided = client.post(
        f"/v1/approvals/{approval['id']}/decision",
        headers=auth("dev-approver"),
        json={"decision": "approved", "reason": "grant already revoked"},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "failed"
    assert decided.json()["error"]["code"] == "permission_denied"
    assert decided.json()["executions"] == []
    assert read_record("alpha")["status"] == "open"

    lookup = _tool(tools, "records_lookup")
    client.post(
        f"/v1/mcp/tools/{lookup['id']}/register",
        headers=auth("dev-registrar"),
        json={
            "risk": "low",
            "permission_key": "records.lookup",
            "verification_kind": "records.lookup",
        },
    )
    disabled = client.post(
        f"/v1/mcp/tools/{lookup['id']}/disable",
        headers=auth("dev-registrar"),
    )
    assert disabled.status_code == 200
    blocked = client.post("/v1/tasks", headers=auth(), json={"request": LOOKUP})
    assert blocked.json()["outcome_kind"] == "clarification"
    assert blocked.json()["executions"] == []


def test_changed_schema_and_uncertain_write(client: TestClient) -> None:
    tools = _register_fixture(client)
    updated = _tool(tools, "records_update")
    client.post(
        f"/v1/mcp/tools/{updated['id']}/register",
        headers=auth("dev-registrar"),
        json={
            "risk": "high",
            "permission_key": "records.update",
            "verification_kind": "records.update",
        },
    )
    created = client.post("/v1/tasks", headers=auth(), json={"request": UPDATE})
    approval_id = created.json()["approvals"][0]["id"]
    spec = client.app.state.registry.get("mcp.fixture.records_update")
    assert spec is not None
    from dataclasses import replace

    client.app.state.registry.replace(replace(spec, version="9", schema_hash="changed-schema"))
    changed = client.post(
        f"/v1/approvals/{approval_id}/decision",
        headers=auth("dev-approver"),
        json={"decision": "approved", "reason": "schema moved"},
    )
    assert changed.json()["status"] == "failed"
    assert changed.json()["error"]["code"] == "schema_changed"
    assert read_record("alpha")["status"] == "open"

    client.app.state.registry.replace(spec)
    set_drop_after_write(True)
    second = client.post(
        "/v1/tasks",
        headers=auth(),
        json={"request": "Update fixture record beta status to released"},
    )
    approval = second.json()["approvals"][0]["id"]
    uncertain = client.post(
        f"/v1/approvals/{approval}/decision",
        headers=auth("dev-approver"),
        json={"decision": "approved", "reason": "apply the beta change"},
    )
    body = uncertain.json()
    assert body["status"] == "uncertain"
    assert body["outcome_kind"] == "uncertain"
    assert body["executions"][0]["status"] == "uncertain"
    assert body["executions"][0]["evidence"]["certainty"] == "uncertain"
    assert read_record("beta")["status"] == "released"
    again = client.get(f"/v1/tasks/{body['id']}", headers=auth())
    assert again.json()["status"] == "uncertain"


def test_delegation_cannot_widen_authority(client: TestClient) -> None:
    expires = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    control = client.post(
        "/v1/delegations",
        headers=auth("dev-approver"),
        json={
            "delegate_subject": "dev-user",
            "permission_key": "authority.approve",
            "resource_scope": "*",
            "expires_at": expires,
        },
    )
    assert control.status_code == 403
    assert control.json()["error"] == "delegation_forbidden"
    self_grant = client.post(
        "/v1/delegations",
        headers=auth(),
        json={
            "delegate_subject": "dev-user",
            "permission_key": "records.lookup",
            "resource_scope": "*",
            "expires_at": expires,
        },
    )
    assert self_grant.status_code == 403
    missing = client.post(
        "/v1/delegations",
        headers=auth("dev-restricted"),
        json={
            "delegate_subject": "dev-user",
            "permission_key": "text.analyze",
            "resource_scope": "*",
            "expires_at": expires,
        },
    )
    assert missing.status_code == 403

    tools = _register_fixture(client)
    lookup = _tool(tools, "records_lookup")
    client.post(
        f"/v1/mcp/tools/{lookup['id']}/register",
        headers=auth("dev-registrar"),
        json={
            "risk": "low",
            "permission_key": "records.lookup",
            "verification_kind": "records.lookup",
        },
    )
    created = client.post(
        "/v1/delegations",
        headers=auth(),
        json={
            "delegate_subject": "dev-restricted",
            "permission_key": "records.lookup",
            "resource_scope": "alpha",
            "expires_at": expires,
        },
    )
    assert created.status_code == 200
    delegated = client.post(
        "/v1/tasks",
        headers=auth("dev-restricted"),
        json={"request": LOOKUP, "delegation_id": created.json()["id"]},
    )
    assert delegated.status_code == 201
    assert delegated.json()["status"] == "completed"
    chained = client.post(
        "/v1/delegations",
        headers=auth("dev-restricted"),
        json={
            "delegate_subject": "dev-denied",
            "permission_key": "records.lookup",
            "resource_scope": "alpha",
            "expires_at": expires,
        },
    )
    assert chained.status_code == 403


def _register_fixture(client: TestClient) -> list[dict[str, object]]:
    added = client.post(
        "/v1/mcp/servers",
        headers=auth("dev-registrar"),
        json={"server_key": "fixture", "display_name": "Fixture"},
    )
    assert added.status_code == 200
    assert added.json()["trust_status"] == "unreviewed"
    discovered = client.post(
        "/v1/mcp/servers/fixture/discover",
        headers=auth("dev-registrar"),
    )
    assert discovered.status_code == 200
    tools = discovered.json()["tools"]
    assert {tool["tool_name"] for tool in tools} == {"records_lookup", "records_update"}
    assert all(tool["registration_state"] == "discovered" for tool in tools)
    assert all(tool["available"] is False for tool in tools)
    return tools


def _tool(tools: list[dict[str, object]], name: str) -> dict[str, object]:
    return next(tool for tool in tools if tool["tool_name"] == name)
