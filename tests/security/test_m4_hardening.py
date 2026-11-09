"""M3 follow-through: destination pinning, proxy minting, secrets, and remote reads."""

from __future__ import annotations

import asyncio

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.conftest import auth
from tests.security.test_reconciliation import UPDATE, _register_update

from apex.config import Settings, assert_runtime_is_allowed
from apex.errors import DomainError
from apex.mcp.fixture import set_drop_after_write
from apex.mcp.remote import _PinnedTransport
from apex.persistence.models import McpServerRecord, McpToolRecord
from apex.secrets import EnvSecretProvider, build_secret_provider


def test_pinned_transport_connects_to_the_validated_address() -> None:
    seen: list[str] = []

    class _Recorder(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            seen.append(request.url.host)
            assert request.extensions["sni_hostname"] == "fixture.test"
            return httpx2.Response(200, content=b"{}")

    transport = _PinnedTransport(
        host="fixture.test",
        port=443,
        addresses=["127.0.0.1"],
        allow_loopback=True,
        max_bytes=1000,
        resolver=lambda _host, _port: ["127.0.0.1"],
        verify=False,
        inner=_Recorder(),
    )
    asyncio.run(transport.handle_async_request(httpx2.Request("GET", "https://fixture.test/mcp")))
    assert seen == ["127.0.0.1"]


def test_pinned_transport_does_not_connect_when_dns_changes() -> None:
    calls = {"count": 0}

    def _resolver(_host: str, _port: int) -> list[str]:
        calls["count"] += 1
        if calls["count"] == 1:
            return ["127.0.0.1"]
        return ["10.9.9.9"]

    class _Recorder(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            raise AssertionError(request.url)

    first = _resolver("fixture.test", 443)
    transport = _PinnedTransport(
        host="fixture.test",
        port=443,
        addresses=first,
        allow_loopback=True,
        max_bytes=1000,
        resolver=_resolver,
        verify=False,
        inner=_Recorder(),
    )
    with pytest.raises(DomainError) as caught:
        asyncio.run(
            transport.handle_async_request(httpx2.Request("GET", "https://fixture.test/mcp"))
        )
    assert caught.value.code == "endpoint_rejected"


def test_production_refuses_env_secrets_when_an_external_provider_is_required() -> None:
    with pytest.raises(RuntimeError, match="secret"):
        assert_runtime_is_allowed(
            Settings(
                _env_file=None,
                environment="production",
                dev_auth_enabled=False,
                seed_dev_principals=False,
                auth_private_key_pem="",
                require_external_secrets=True,
            )
        )


def test_env_provider_resolves_without_storing_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_TOKEN", "local-test-token")
    provider = build_secret_provider(Settings(_env_file=None, require_external_secrets=False))
    assert isinstance(provider, EnvSecretProvider)
    assert provider.resolve("env:MCP_TOKEN") == "local-test-token"


def test_remote_tool_without_a_local_read_stays_inconclusive(
    client: TestClient, session: Session
) -> None:
    _register_update(client)
    set_drop_after_write(True)
    lost = client.post("/v1/tasks", headers=auth(), json={"request": UPDATE})
    approval_id = lost.json()["approvals"][0]["id"]
    body = client.post(
        f"/v1/approvals/{approval_id}/decision",
        headers=auth("dev-approver"),
        json={"decision": "approved", "reason": "apply it"},
    )
    assert body.json()["status"] == "uncertain"
    execution_id = body.json()["executions"][0]["id"]
    server = session.scalar(select(McpServerRecord).where(McpServerRecord.server_key == "fixture"))
    tool = session.scalar(select(McpToolRecord).where(McpToolRecord.tool_name == "records_update"))
    assert server is not None and tool is not None
    server.transport = "http"
    tool.reconciles_capability_id = tool.capability_id
    tool.available = True
    session.commit()
    reconciled = client.post(f"/v1/executions/{execution_id}/reconcile", headers=auth())
    assert reconciled.status_code == 200
    assert reconciled.json()["outcome_certainty"] == "inconclusive"
    assert reconciled.json()["evidence"]["observation"] == "remote_read_not_independent"
    assert "Do not retry" in reconciled.json()["evidence"]["manual_reconciliation"]
    follow = client.get(f"/v1/tasks/{body.json()['id']}", headers=auth())
    assert follow.json()["status"] == "uncertain"
