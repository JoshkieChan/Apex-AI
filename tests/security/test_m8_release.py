"""Release controls. Suspension stops new work and leaves completed work readable."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from tests.conftest import ANALYZE, auth

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = ("BEGIN PRIVATE KEY", "BEGIN OPENSSH PRIVATE KEY", "AKIA")


def test_execution_hold_blocks_new_work_and_keeps_the_completed_task(client: TestClient) -> None:
    first = client.post("/v1/tasks", headers=auth("dev-user"), json={"request": ANALYZE})
    assert first.status_code == 201
    assert first.json()["status"] == "completed"
    held = client.post(
        "/v1/operations/execution",
        headers=auth("dev-approver"),
        json={"suspended": True},
    )
    assert held.status_code == 200
    assert held.json()["suspended"] is True
    health = client.get("/v1/health")
    assert health.json()["execution_suspended"] is True
    denied = client.post(
        "/v1/operations/execution",
        headers=auth("dev-user"),
        json={"suspended": False},
    )
    assert denied.status_code == 403
    second = client.post("/v1/tasks", headers=auth("dev-user"), json={"request": ANALYZE})
    assert second.status_code == 201
    body = second.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "execution_suspended"
    assert body["executions"] == []
    stored = client.get(f"/v1/tasks/{first.json()['id']}", headers=auth("dev-user"))
    assert stored.json()["status"] == "completed"
    resumed = client.post(
        "/v1/operations/execution",
        headers=auth("dev-approver"),
        json={"suspended": False},
    )
    assert resumed.json()["suspended"] is False
    third = client.post("/v1/tasks", headers=auth("dev-user"), json={"request": ANALYZE})
    assert third.json()["status"] == "completed"


def test_execution_hold_cannot_be_delegated(client: TestClient) -> None:
    response = client.post(
        "/v1/delegations",
        headers=auth("dev-approver"),
        json={
            "delegate_subject": "dev-user",
            "permission_key": "operations.suspend",
            "resource_scope": "*",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 403
    assert response.json()["error"] == "delegation_forbidden"


def test_source_tree_has_no_private_key_material() -> None:
    roots = [ROOT / "src", ROOT / "docs", ROOT / "infrastructure", ROOT / ".env.example"]
    for root in roots:
        paths = [root] if root.is_file() else list(root.rglob("*"))
        for path in paths:
            if not path.is_file() or path.suffix in {".png", ".wav"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for marker in FORBIDDEN:
                assert marker not in text, path
