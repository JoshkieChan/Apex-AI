"""Remote MCP registration stays behind Apex authority, TLS, and endpoint checks."""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address
from pathlib import Path

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.orm import Session
from tests.conftest import auth

from apex.api.app import create_app
from apex.config import Settings
from apex.errors import DomainError
from apex.mcp.fixture import server
from apex.mcp.remote import invoke_remote_tool
from apex.netpolicy import validate_remote_url
from apex.persistence.models import McpServerRecord


def test_remote_registration_rejects_untrusted_destinations(client: TestClient) -> None:
    private = client.post(
        "/v1/mcp/servers",
        headers=auth("dev-registrar"),
        json={
            "server_key": "remote",
            "display_name": "Remote",
            "transport": "http",
            "endpoint": "https://10.1.2.3/mcp",
            "credential_ref": "env:MCP_TOKEN",
        },
    )
    assert private.status_code == 400
    assert private.json()["error"] == "endpoint_rejected"
    loopback = client.post(
        "/v1/mcp/servers",
        headers=auth("dev-registrar"),
        json={
            "server_key": "remote",
            "display_name": "Remote",
            "transport": "http",
            "endpoint": "http://127.0.0.1:9/mcp",
        },
    )
    assert loopback.status_code == 400


def test_secret_reference_is_not_the_secret(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_TOKEN", "super-secret-token")
    created = client.post(
        "/v1/mcp/servers",
        headers=auth("dev-registrar"),
        json={
            "server_key": "remote",
            "display_name": "Remote",
            "transport": "http",
            "endpoint": "https://1.1.1.1/mcp",
            "credential_ref": "env:MCP_TOKEN",
        },
    )
    assert created.status_code == 200
    body = created.text
    assert "super-secret-token" not in body
    stored = session.query(McpServerRecord).filter_by(server_key="remote").one()
    assert stored.credential_ref == "env:MCP_TOKEN"
    assert stored.endpoint == "https://1.1.1.1/mcp"
    assert "super-secret-token" not in (stored.credential_ref or "")


def test_local_fixture_serves_discovery_without_granting_execution(
    tmp_path: Path, engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, ca = _start_https_fixture(tmp_path)
    monkeypatch.setenv("APEX_MCP_CA_FILE", str(ca))
    monkeypatch.setenv("MCP_TOKEN", "fixture-secret")
    settings = Settings(
        _env_file=None,
        environment="test",
        dev_auth_enabled=True,
        auth_issuer="https://apex.test/issuer",
        auth_audience="apex-api",
        auth_public_key_pem=_public(),
        auth_private_key_pem=_private(),
        seed_dev_principals=True,
        mcp_allow_local_fixture=True,
        database_url="sqlite+pysqlite:///:memory:",
        model_provider="mock",
    )
    from sqlalchemy.engine import Engine

    assert isinstance(engine, Engine)
    application = create_app(settings=settings, engine=engine)
    with TestClient(application) as api:
        created = api.post(
            "/v1/mcp/servers",
            headers=auth("dev-registrar"),
            json={
                "server_key": "remote",
                "display_name": "Remote fixture",
                "transport": "http",
                "endpoint": url,
                "credential_ref": "env:MCP_TOKEN",
            },
        )
        assert created.status_code == 200
        assert "fixture-secret" not in created.text
        discovered = api.post("/v1/mcp/servers/remote/discover", headers=auth("dev-registrar"))
        assert discovered.status_code == 200
        tools = discovered.json()["tools"]
        assert tools
        assert all(tool["available"] is False for tool in tools)
        lookup = next(tool for tool in tools if tool["tool_name"] == "records_lookup")
        blocked = api.post(
            "/v1/tasks",
            headers=auth(),
            json={"request": "Look up fixture record alpha"},
        )
        assert blocked.json()["executions"] == []
        registered = api.post(
            f"/v1/mcp/tools/{lookup['id']}/register",
            headers=auth("dev-registrar"),
            json={
                "risk": "low",
                "permission_key": "records.lookup",
                "verification_kind": "records.lookup",
            },
        )
        assert registered.status_code == 200
        executed = api.post(
            "/v1/tasks",
            headers=auth(),
            json={"request": "Look up fixture record alpha"},
        )
        assert executed.status_code == 201
        payload = executed.json()
        assert payload["status"] == "completed", payload.get("error")


def test_certificate_failure_and_rebinding(tmp_path: Path) -> None:
    url, _ca = _start_https_fixture(tmp_path)
    settings = Settings(
        _env_file=None,
        environment="test",
        mcp_allow_local_fixture=True,
        model_provider="mock",
        database_url="sqlite+pysqlite:///:memory:",
    )
    os.environ.pop("APEX_MCP_CA_FILE", None)
    with pytest.raises(DomainError):
        invoke_remote_tool(
            endpoint=url,
            credential_ref=None,
            tool_name="records_lookup",
            arguments={"name": "alpha"},
            timeout_seconds=2,
            settings=settings,
        )

    def resolver(host: str, port: int) -> list[str]:
        del host, port
        return ["10.9.9.9"]

    with pytest.raises(DomainError) as rebound:
        validate_remote_url(
            "https://fixture.test/mcp",
            settings,
            allow_local_fixture=True,
            resolver=resolver,
        )
    assert rebound.value.code == "endpoint_rejected"
    with pytest.raises(DomainError):
        validate_remote_url(
            "http://127.0.0.1/mcp",
            Settings(
                _env_file=None,
                environment="production",
                model_provider="mock",
                database_url="sqlite+pysqlite:///:memory:",
            ),
            allow_local_fixture=True,
        )


def _start_https_fixture(tmp_path: Path) -> tuple[str, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(IPv4Address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path = tmp_path / "key.pem"
    cert_path = tmp_path / "cert.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    app = server().streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
    )
    http = uvicorn.Server(config)
    thread = threading.Thread(target=http.run, daemon=True)
    thread.start()
    deadline = datetime.now(UTC) + timedelta(seconds=5)
    while datetime.now(UTC) < deadline:
        if http.started:
            return f"https://127.0.0.1:{port}/mcp", cert_path
        threading.Event().wait(0.05)
    raise RuntimeError("The local MCP fixture did not start.")


def _public() -> str:
    from tests.support_auth import PUBLIC_PEM

    return PUBLIC_PEM


def _private() -> str:
    from tests.support_auth import PRIVATE_PEM

    return PRIVATE_PEM
