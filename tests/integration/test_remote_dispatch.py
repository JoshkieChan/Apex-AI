"""Remote MCP transport phase decides execution certainty. A dropped response is not a failure."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address
from pathlib import Path

import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import func, select
from tests.conftest import auth
from tests.integration.postgres_db import disposable_database
from tests.security.test_coordination import SEQUENTIAL
from tests.support_auth import AUDIENCE, ISSUER, PRIVATE_PEM, PUBLIC_PEM

from apex.api.app import create_app
from apex.authority.engine import AuthorityEngine
from apex.config import Settings
from apex.contracts.schemas import Decision, PlanStepDraft, RiskLevel
from apex.mcp.fixture import (
    discover_fixture_tools,
    operation_landed,
    read_record,
    reset_fixture,
    server,
)
from apex.mcp.registration import McpRegistrationService
from apex.persistence.base import Base
from apex.persistence.models import Execution, Goal, GoalSubtask, PlanStep
from apex.persistence.session import create_db_engine
from apex.planning.hashes import hashes_for_steps
from apex.worker import recover_available

ROOT = Path(__file__).resolve().parents[2]


def test_dispatched_remote_update_stays_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"update": 0}
    url, ca = _serve(tmp_path / "update", _fault_after_apply(calls, b"records_update", "drop"))
    monkeypatch.setenv("APEX_MCP_CA_FILE", str(ca))
    _with_database(
        tmp_path,
        lambda client: _assert_dispatched_update(
            client,
            url,
            calls,
            monkeypatch,
            {"endpoint_timeout", "capability_unavailable"},
        ),
    )


@pytest.mark.parametrize("mode", ["oversize", "html", "unstructured"])
def test_post_dispatch_validation_stays_uncertain(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"update": 0}
    url, ca = _serve(tmp_path / mode, _fault_after_apply(calls, b"records_update", mode))
    monkeypatch.setenv("APEX_MCP_CA_FILE", str(ca))
    codes = {
        "oversize": {"endpoint_rejected"},
        "html": {"endpoint_rejected"},
        "unstructured": {"invalid_output", "capability_unavailable", "execution_failed"},
    }[mode]
    _with_database(
        tmp_path,
        lambda client: _assert_dispatched_update(client, url, calls, monkeypatch, codes),
    )


def test_server_reported_tool_error_after_write_stays_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"update": 0}
    url, ca = _serve(
        tmp_path / "tool-error", _fault_after_apply(calls, b"records_update", "tool_error")
    )
    monkeypatch.setenv("APEX_MCP_CA_FILE", str(ca))
    _with_database(
        tmp_path,
        lambda client: _assert_dispatched_update(
            client, url, calls, monkeypatch, {"capability_failed"}
        ),
    )


def test_pre_dispatch_remote_failure_is_definitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("APEX_MCP_CA_FILE", raising=False)
    _with_database(tmp_path, lambda client: _assert_pre_dispatch(client, monkeypatch))


def test_idempotent_remote_lookup_timeout_is_not_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"lookup": 0}
    url, ca = _serve(tmp_path / "lookup", _drop_after(calls, b"records_lookup"))
    monkeypatch.setenv("APEX_MCP_CA_FILE", str(ca))
    _with_database(tmp_path, lambda client: _assert_lookup_timeout(client, url, calls))


def test_idempotent_remote_response_rejection_is_not_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"lookup": 0}
    url, ca = _serve(tmp_path / "lookup-html", _fault_after_apply(calls, b"records_lookup", "html"))
    monkeypatch.setenv("APEX_MCP_CA_FILE", str(ca))
    _with_database(tmp_path, lambda client: _assert_lookup_rejected(client, url, calls))


def _assert_dispatched_update(
    client: TestClient,
    endpoint: str,
    calls: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    codes: set[str],
) -> None:
    reset_fixture()
    try:
        _assert_applied_update(client, endpoint, calls, monkeypatch, codes)
    finally:
        reset_fixture()


def _assert_applied_update(
    client: TestClient,
    endpoint: str,
    calls: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    codes: set[str],
) -> None:
    capability = _register(client, endpoint, "records_update", "high", "records.update")
    goal = _goal(client)
    _retarget(
        client,
        str(goal["id"]),
        capability,
        {"name": "alpha", "status": "closed", "operation_id": "op-remote-1"},
    )
    _extend_timeout(client, capability)
    monkeypatch.setattr(AuthorityEngine, "evaluate", _allow)
    ran = client.post(f"/v1/goals/{goal['id']}/run", headers=auth())
    assert ran.status_code == 200, ran.text
    body = ran.json()
    assert body["status"] == "uncertain", {"body": body, "calls": calls}
    subtask = next(row for row in body["subtasks"] if row["key"] == "s0")
    assert subtask["status"] == "uncertain"
    assert subtask["execution_id"]
    assert calls["update"] == 1
    assert read_record("alpha") == {"name": "alpha", "status": "closed"}
    assert operation_landed("op-remote-1") == {"name": "alpha", "status": "closed"}
    execution = _execution(client, subtask["execution_id"])
    assert execution.status == "uncertain"
    assert execution.outcome_certainty == "possibly_sent"
    assert execution.error_code in codes
    assert (execution.evidence or {}).get("transport_phase") == "dispatched"
    inspected = client.get(f"/v1/goals/{goal['id']}", headers=auth())
    assert inspected.status_code == 200, inspected.text
    again = client.post(f"/v1/goals/{goal['id']}/run", headers=auth())
    assert again.status_code == 200, again.text
    revised = client.post(f"/v1/goals/{goal['id']}/replan", headers=auth())
    assert revised.status_code == 409
    assert revised.json()["error"] == "outcome_uncertain"
    report = recover_available(
        factory=client.app.state.session_factory,
        settings=client.app.state.settings,
        registry=client.app.state.registry,
        provider=client.app.state.provider,
        clock=client.app.state.clock,
    )
    assert report.goals_succeeded == 0
    _reopen(client, str(goal["id"]))
    forced = client.post(f"/v1/goals/{goal['id']}/run", headers=auth())
    assert forced.status_code == 200, forced.text
    assert forced.json()["status"] == "uncertain"
    assert calls["update"] == 1
    assert read_record("alpha") == {"name": "alpha", "status": "closed"}
    assert operation_landed("op-remote-1") == {"name": "alpha", "status": "closed"}
    assert _execution_count(client, subtask["execution_id"]) == 1


def _assert_pre_dispatch(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = f"http://127.0.0.1:{_closed_port()}/mcp"
    capability = _register_closed(client, endpoint)
    goal = _goal(client)
    _retarget(
        client,
        str(goal["id"]),
        capability,
        {"name": "alpha", "status": "closed", "operation_id": "op-remote-1"},
    )
    monkeypatch.setattr(AuthorityEngine, "evaluate", _allow)
    ran = client.post(f"/v1/goals/{goal['id']}/run", headers=auth())
    assert ran.status_code == 200, ran.text
    body = ran.json()
    assert body["status"] != "uncertain"
    subtask = next(row for row in body["subtasks"] if row["key"] == "s0")
    assert subtask["status"] == "failed", body
    assert subtask["execution_id"]
    execution = _execution(client, subtask["execution_id"])
    assert execution.status == "failed"
    assert execution.outcome_certainty == "not_sent"
    assert (execution.evidence or {}).get("transport_phase") == "pre_dispatch"


def _assert_lookup_timeout(client: TestClient, endpoint: str, calls: dict[str, int]) -> None:
    capability = _register(client, endpoint, "records_lookup", "low", "records.lookup")
    goal = _goal(client)
    _retarget(client, str(goal["id"]), capability, {"name": "alpha"})
    _extend_timeout(client, capability)
    ran = client.post(f"/v1/goals/{goal['id']}/run", headers=auth())
    assert ran.status_code == 200, ran.text
    body = ran.json()
    assert body["status"] != "uncertain"
    subtask = next(row for row in body["subtasks"] if row["key"] == "s0")
    assert subtask["status"] == "failed", body
    execution = _execution(client, subtask["execution_id"])
    assert execution.status == "failed"
    assert execution.outcome_certainty == "confirmed_failure"
    assert execution.error_code in {"endpoint_timeout", "capability_unavailable"}
    assert calls["lookup"] == 1


def _assert_lookup_rejected(client: TestClient, endpoint: str, calls: dict[str, int]) -> None:
    capability = _register(client, endpoint, "records_lookup", "low", "records.lookup")
    goal = _goal(client)
    _retarget(client, str(goal["id"]), capability, {"name": "alpha"})
    ran = client.post(f"/v1/goals/{goal['id']}/run", headers=auth())
    assert ran.status_code == 200, ran.text
    body = ran.json()
    assert body["status"] != "uncertain"
    subtask = next(row for row in body["subtasks"] if row["key"] == "s0")
    assert subtask["status"] == "failed", body
    execution = _execution(client, subtask["execution_id"])
    assert execution.status == "failed"
    assert execution.outcome_certainty == "confirmed_failure"
    assert execution.error_code == "endpoint_rejected"
    assert calls["lookup"] == 1


def _extend_timeout(client: TestClient, capability_id: str) -> None:
    spec = client.app.state.registry.get(capability_id)
    assert spec is not None
    client.app.state.registry.replace(replace(spec, timeout_seconds=30.0))


def _allow(
    self: AuthorityEngine, principal_id: str, permission_key: str, **kwargs: object
) -> Decision:
    del self, principal_id, permission_key, kwargs
    return Decision.allow


def _goal(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/v1/goals",
        headers=auth(),
        params={"max_waves": 0},
        json={"objective": SEQUENTIAL},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _register(client: TestClient, endpoint: str, tool_name: str, risk: str, permission: str) -> str:
    created = client.post(
        "/v1/mcp/servers",
        headers=auth("dev-registrar"),
        json={
            "server_key": tool_name,
            "display_name": tool_name,
            "transport": "http",
            "endpoint": endpoint,
        },
    )
    assert created.status_code == 200, created.text
    discovered = client.post(f"/v1/mcp/servers/{tool_name}/discover", headers=auth("dev-registrar"))
    assert discovered.status_code == 200, discovered.text
    tool = next(item for item in discovered.json()["tools"] if item["tool_name"] == tool_name)
    registered = client.post(
        f"/v1/mcp/tools/{tool['id']}/register",
        headers=auth("dev-registrar"),
        json={"risk": risk, "permission_key": permission, "verification_kind": permission},
    )
    assert registered.status_code == 200, registered.text
    return str(registered.json()["capability_id"])


def _register_closed(client: TestClient, endpoint: str) -> str:
    created = client.post(
        "/v1/mcp/servers",
        headers=auth("dev-registrar"),
        json={
            "server_key": "closed",
            "display_name": "Closed",
            "transport": "http",
            "endpoint": endpoint,
        },
    )
    assert created.status_code == 200, created.text
    with client.app.state.session_factory() as session:
        McpRegistrationService(
            session, client.app.state.registry, client.app.state.settings
        ).discover("closed", discover_fixture_tools(), datetime.now(UTC))
        session.commit()
    listed = client.get("/v1/mcp/tools", headers=auth("dev-registrar"))
    tool = next(item for item in listed.json()["tools"] if item["tool_name"] == "records_update")
    registered = client.post(
        f"/v1/mcp/tools/{tool['id']}/register",
        headers=auth("dev-registrar"),
        json={
            "risk": "high",
            "permission_key": "records.update",
            "verification_kind": "records.update",
        },
    )
    assert registered.status_code == 200, registered.text
    return str(registered.json()["capability_id"])


def _retarget(
    client: TestClient, goal_id: str, capability_id: str, payload: dict[str, str]
) -> None:
    spec = client.app.state.registry.get(capability_id)
    assert spec is not None
    with client.app.state.session_factory() as session:
        goal = session.get(Goal, goal_id)
        subtask = session.scalar(
            select(GoalSubtask).where(
                GoalSubtask.goal_id == goal_id, GoalSubtask.subtask_key == "s0"
            )
        )
        assert goal is not None and subtask is not None and subtask.plan_step_id
        step = session.get(PlanStep, subtask.plan_step_id)
        assert step is not None
        step.capability_id = spec.id
        step.permission_key = spec.permission_key
        step.risk = spec.risk.value
        step.verification_kind = spec.verification_kind
        step.capability_version = spec.version
        step.schema_hash = spec.schema_hash
        step.input_payload = payload
        subtask.capability_id = spec.id
        subtask.permission_key = spec.permission_key
        subtask.input_payload = payload
        draft = PlanStepDraft(
            step_index=step.step_index,
            capability_id=step.capability_id,
            objective=step.objective,
            input_payload=dict(step.input_payload),
            input_bindings=dict(step.input_bindings or {}),
            depends_on=[int(item) for item in step.depends_on],
            permission_key=step.permission_key,
            risk=RiskLevel(step.risk),
            verification_kind=step.verification_kind,
            failure_policy=dict(step.failure_policy),
            capability_version=step.capability_version,
            schema_hash=step.schema_hash,
        )
        step.operation_hash = hashes_for_steps([draft], principal_id=goal.principal_id)[
            step.step_index
        ]
        session.commit()


def _reopen(client: TestClient, goal_id: str) -> None:
    """Put an uncertain goal back into the recoverable set without clearing its execution."""
    with client.app.state.session_factory() as session:
        goal = session.get(Goal, goal_id)
        subtask = session.scalar(
            select(GoalSubtask).where(
                GoalSubtask.goal_id == goal_id, GoalSubtask.subtask_key == "s0"
            )
        )
        assert goal is not None and subtask is not None
        goal.status = "running"
        goal.lease_owner = None
        goal.lease_expires_at = None
        subtask.status = "pending"
        subtask.error_code = None
        session.commit()


def _execution(client: TestClient, execution_id: object) -> Execution:
    with client.app.state.session_factory() as session:
        row = session.get(Execution, str(execution_id))
        assert row is not None
        session.expunge(row)
        return row


def _execution_count(client: TestClient, execution_id: object) -> int:
    with client.app.state.session_factory() as session:
        row = session.get(Execution, str(execution_id))
        assert row is not None
        return int(
            session.scalar(
                select(func.count())
                .select_from(Execution)
                .where(Execution.plan_step_id == row.plan_step_id)
            )
            or 0
        )


def _with_database(tmp_path: Path, scenario: Callable[[TestClient], None]) -> None:
    del tmp_path
    postgres = os.environ.get("APEX_POSTGRES_TEST_URL")
    if not postgres:
        engine = create_db_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        _run(engine, "sqlite+pysqlite:///:memory:", scenario)
        return
    name = f"apex_rm_{os.getpid() % 10000}_{id(scenario) % 100000}"
    with disposable_database(name) as url:
        previous = os.environ.get("APEX_DATABASE_URL")
        os.environ["APEX_DATABASE_URL"] = url
        try:
            command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        finally:
            if previous is None:
                os.environ.pop("APEX_DATABASE_URL", None)
            else:
                os.environ["APEX_DATABASE_URL"] = previous
        engine = create_db_engine(url)
        _run(engine, url, scenario)


def _run(engine: object, url: str, scenario: Callable[[TestClient], None]) -> None:
    application = create_app(settings=_settings(url), engine=engine)  # type: ignore[arg-type]
    try:
        with TestClient(application) as client:
            scenario(client)
    finally:
        application.state.engine.dispose()


def _settings(url: str) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        dev_auth_enabled=True,
        auth_issuer=ISSUER,
        auth_audience=AUDIENCE,
        auth_public_key_pem=PUBLIC_PEM,
        auth_private_key_pem=PRIVATE_PEM,
        seed_dev_principals=True,
        database_url=url,
        model_provider="mock",
        mcp_allow_local_fixture=True,
        instance_id="remote-test",
    )


def _fault_after_apply(
    calls: dict[str, int], marker: bytes, mode: str
) -> Callable[[object, dict, Callable], object]:
    """Run the real tool, then drop or replace the response. The write stays applied."""

    async def middleware(scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        messages: list[dict] = []
        body = b""
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            body += message.get("body") or b""
            if not message.get("more_body", False):
                break
        if scope.get("method") != "POST" or b"tools/call" not in body or marker not in body:
            await app(scope, _replay(messages), send)
            return
        captured: list[dict] = []

        async def capture(message: dict) -> None:
            captured.append(message)

        await app(scope, _replay(messages), capture)
        key = "update" if marker == b"records_update" else "lookup"
        calls[key] += 1
        if mode == "drop":
            await asyncio.Event().wait()
            return
        payload = _response_body(captured)
        content_type = b"application/json"
        if mode == "oversize":
            payload = b'{"padding":"' + (b"x" * 1_000_001) + b'"}'
        elif mode == "html":
            payload = b"<html>rejected</html>"
            content_type = b"text/html"
        elif mode == "unstructured":
            payload = _rewrite_result(payload, structured=False, tool_error=False)
        elif mode == "tool_error":
            payload = _rewrite_result(payload, structured=True, tool_error=True)
        else:
            raise AssertionError(mode)
        await _send_replaced(send, captured, payload, content_type)

    app = server().streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return middleware


def _replay(messages: list[dict]) -> Callable[[], dict]:
    index = 0

    async def replay() -> dict:
        nonlocal index
        if index < len(messages):
            message = messages[index]
            index += 1
            return message
        return {"type": "http.request", "body": b"", "more_body": False}

    return replay


def _response_body(messages: list[dict]) -> bytes:
    return b"".join(
        message.get("body") or b""
        for message in messages
        if message.get("type") == "http.response.body"
    )


def _rewrite_result(payload: bytes, *, structured: bool, tool_error: bool) -> bytes:
    parsed = json.loads(payload)
    result = parsed["result"]
    if tool_error:
        result["isError"] = True
    if not structured:
        result["structuredContent"] = "not-an-object"
        result["isError"] = False
    return json.dumps(parsed).encode("utf-8")


async def _send_replaced(
    send: Callable, captured: list[dict], payload: bytes, content_type: bytes
) -> None:
    start = next(message for message in captured if message.get("type") == "http.response.start")
    headers: list[list[bytes]] = []
    for name, value in start.get("headers", []):
        if name.lower() in {b"content-type", b"content-length"}:
            continue
        headers.append([name, value])
    headers.append([b"content-type", content_type])
    headers.append([b"content-length", str(len(payload)).encode("ascii")])
    await send({**start, "headers": headers})
    await send({"type": "http.response.body", "body": payload, "more_body": False})


def _drop_after(calls: dict[str, int], marker: bytes) -> Callable[[object, dict, Callable], object]:
    async def middleware(scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        messages: list[dict] = []
        body = b""
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            body += message.get("body") or b""
            if not message.get("more_body", False):
                break
        if scope.get("method") == "POST" and b"tools/call" in body and marker in body:
            key = "update" if marker == b"records_update" else "lookup"
            calls[key] += 1
            await asyncio.Event().wait()
            return
        index = 0

        async def replay() -> dict:
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await app(scope, replay, send)

    app = server().streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return middleware


def _serve(tmp_path: Path, application: object) -> tuple[str, Path]:
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
    tmp_path.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    config = uvicorn.Config(
        application,
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


def _closed_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port
