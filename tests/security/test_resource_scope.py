"""Delegation scope is enforced from the payload. The planner does not grant it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.conftest import auth

from apex.authority.scope import enforce_resource_scope
from apex.errors import DomainError
from apex.persistence.models import Delegation

LOOKUP = "Look up fixture record alpha"
BETA = "Look up fixture record beta"
ANALYZE = "Analyze the following text and report its word count: authorized everywhere"


def test_scope_parser_fails_closed_for_mixed_and_missing_references() -> None:
    enforce_resource_scope("alpha", {"name": "alpha"}, ("name",))
    enforce_resource_scope("*", {"name": ["alpha", "beta"]}, ("name",))
    with pytest.raises(DomainError) as mixed:
        enforce_resource_scope("alpha", {"name": ["alpha", "beta"]}, ("name",))
    assert mixed.value.code == "scope_violation"
    with pytest.raises(DomainError) as missing:
        enforce_resource_scope("alpha", {"authorized": True}, ("name",))
    assert missing.value.code == "scope_violation"
    with pytest.raises(DomainError):
        enforce_resource_scope("alpha", {"text": "authorized"}, ())


def test_delegated_lookup_is_limited_to_named_records(client: TestClient, session: Session) -> None:
    _register_lookup(client)
    direct = client.post("/v1/tasks", headers=auth(), json={"request": BETA})
    assert direct.status_code == 201
    assert direct.json()["status"] == "completed"

    created = _delegate(client, "alpha")
    allowed = client.post(
        "/v1/tasks",
        headers=auth("dev-restricted"),
        json={"request": LOOKUP, "delegation_id": created},
    )
    assert allowed.status_code == 201
    assert allowed.json()["status"] == "completed"

    denied = client.post(
        "/v1/tasks",
        headers=auth("dev-restricted"),
        json={"request": BETA, "delegation_id": created},
    )
    assert denied.status_code == 201
    assert denied.json()["status"] == "failed"
    assert denied.json()["error"]["code"] == "scope_violation"
    assert denied.json()["executions"] == []


def test_expired_revoked_and_withdrawn_delegations_fail(
    client: TestClient, session: Session
) -> None:
    _register_lookup(client)
    expired = _delegate(client, "alpha")
    row = session.get(Delegation, expired)
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    session.commit()
    expired_task = client.post(
        "/v1/tasks",
        headers=auth("dev-restricted"),
        json={"request": LOOKUP, "delegation_id": expired},
    )
    assert expired_task.status_code == 403
    assert expired_task.json()["error"] == "delegation_forbidden"

    active = _delegate(client, "alpha")
    revoked = session.get(Delegation, active)
    assert revoked is not None
    revoked.revoked_at = datetime.now(UTC)
    session.commit()
    revoked_task = client.post(
        "/v1/tasks",
        headers=auth("dev-restricted"),
        json={"request": LOOKUP, "delegation_id": active},
    )
    assert revoked_task.status_code == 403
    assert revoked_task.json()["error"] == "delegation_forbidden"

    replacement = _delegate(client, "alpha")
    client.post(
        "/v1/grants/revoke",
        headers=auth("dev-approver"),
        json={"subject": "dev-user", "permission_key": "records.lookup"},
    )
    withdrawn = client.post(
        "/v1/tasks",
        headers=auth("dev-restricted"),
        json={"request": LOOKUP, "delegation_id": replacement},
    )
    assert withdrawn.json()["status"] == "failed"
    assert withdrawn.json()["error"]["code"] == "permission_denied"


def test_scope_change_invalidates_the_approval(client: TestClient, session: Session) -> None:
    tools = _register_lookup(client)
    updated = next(tool for tool in tools if tool["tool_name"] == "records_update")
    client.post(
        f"/v1/mcp/tools/{updated['id']}/register",
        headers=auth("dev-registrar"),
        json={
            "risk": "high",
            "permission_key": "records.update",
            "verification_kind": "records.update",
        },
    )
    delegation_id = _delegate(
        client, "alpha", permission="records.update", delegate="dev-restricted"
    )
    created = client.post(
        "/v1/tasks",
        headers=auth("dev-restricted"),
        json={
            "request": "Update fixture record alpha status to closed",
            "delegation_id": delegation_id,
        },
    )
    assert created.json()["status"] == "awaiting_approval"
    row = session.get(Delegation, delegation_id)
    assert row is not None
    row.resource_scope = "beta"
    session.commit()
    approval_id = created.json()["approvals"][0]["id"]
    decided = client.post(
        f"/v1/approvals/{approval_id}/decision",
        headers=auth("dev-approver"),
        json={"decision": "approved", "reason": "scope changed underneath"},
    )
    assert decided.json()["status"] == "failed"
    assert decided.json()["error"]["code"] == "approval_mismatch"
    assert decided.json()["executions"] == []


def test_expiry_after_planning_is_rejected_at_execution(
    client: TestClient, session: Session
) -> None:
    tools = _register_lookup(client)
    updated = next(tool for tool in tools if tool["tool_name"] == "records_update")
    client.post(
        f"/v1/mcp/tools/{updated['id']}/register",
        headers=auth("dev-registrar"),
        json={
            "risk": "high",
            "permission_key": "records.update",
            "verification_kind": "records.update",
        },
    )
    delegation_id = _delegate(client, "alpha", permission="records.update")
    created = client.post(
        "/v1/tasks",
        headers=auth("dev-restricted"),
        json={
            "request": "Update fixture record alpha status to closed",
            "delegation_id": delegation_id,
        },
    )
    assert created.json()["status"] == "awaiting_approval"
    row = session.get(Delegation, delegation_id)
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    session.commit()
    decided = client.post(
        f"/v1/approvals/{created.json()['approvals'][0]['id']}/decision",
        headers=auth("dev-approver"),
        json={"decision": "approved", "reason": "delegation already expired"},
    )
    assert decided.json()["status"] == "failed"
    assert decided.json()["error"]["code"] == "permission_denied"
    assert decided.json()["executions"] == []


def test_narrow_scope_cannot_analyze_without_a_resource_reference(client: TestClient) -> None:
    delegation_id = _delegate(client, "alpha", permission="text.analyze")
    denied = client.post(
        "/v1/tasks",
        headers=auth("dev-restricted"),
        json={"request": ANALYZE, "delegation_id": delegation_id},
    )
    assert denied.json()["status"] == "failed"
    assert denied.json()["error"]["code"] == "scope_violation"


def _register_lookup(client: TestClient) -> list[dict[str, object]]:
    client.post(
        "/v1/mcp/servers",
        headers=auth("dev-registrar"),
        json={"server_key": "fixture", "display_name": "Fixture"},
    )
    discovered = client.post("/v1/mcp/servers/fixture/discover", headers=auth("dev-registrar"))
    tools = discovered.json()["tools"]
    lookup = next(tool for tool in tools if tool["tool_name"] == "records_lookup")
    client.post(
        f"/v1/mcp/tools/{lookup['id']}/register",
        headers=auth("dev-registrar"),
        json={
            "risk": "low",
            "permission_key": "records.lookup",
            "verification_kind": "records.lookup",
        },
    )
    return tools


def _delegate(
    client: TestClient,
    scope: str,
    *,
    permission: str = "records.lookup",
    delegate: str = "dev-restricted",
) -> str:
    created = client.post(
        "/v1/delegations",
        headers=auth(),
        json={
            "delegate_subject": delegate,
            "permission_key": permission,
            "resource_scope": scope,
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        },
    )
    assert created.status_code == 200
    return str(created.json()["id"])
