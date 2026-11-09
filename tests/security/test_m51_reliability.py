"""Autonomous recovery, verification integrity, secrets, and memory purge."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from tests.conftest import auth
from tests.security.test_coordination import FAIL, PARALLEL, SEQUENTIAL, _goal
from tests.support_auth import AUDIENCE, ISSUER, PUBLIC_PEM

from apex.api.app import create_app
from apex.config import Settings, assert_runtime_is_allowed
from apex.coordination.leases import claim_goal
from apex.errors import DomainError
from apex.identity.oidc import discover_jwks_uri
from apex.identity.tokens import TokenAuthenticator
from apex.mcp.remote import MCP_MAX_REDIRECTS
from apex.netpolicy import fetch_bounded
from apex.persistence.models import (
    Approval,
    ContextRecord,
    Execution,
    Goal,
    GoalSubtask,
    MemoryPurge,
    MemoryRecord,
    MemoryRevision,
    PlanStep,
)
from apex.runtime_ids import new_id
from apex.secrets import FileSecretProvider, VaultSecretProvider
from apex.verification.engine import assess_claim, verify_artifact, verify_execution
from apex.worker import recover_available


def test_a_persisted_goal_resumes_without_run(client: TestClient) -> None:
    started = _goal(client, SEQUENTIAL, max_waves=0)
    assert started["status"] == "running"
    assert all(row["execution_id"] is None for row in started["subtasks"])
    assert _recover(client) >= 1
    finished = client.get(f"/v1/goals/{started['id']}", headers=auth()).json()
    assert finished["status"] == "completed"
    assert finished["verifications"][0]["status"] == "passed"
    assert finished["verifications"][0]["revision"] == finished["revision"]


def test_completed_work_is_not_repeated_after_restart(client: TestClient) -> None:
    started = _goal(client, SEQUENTIAL, max_waves=1)
    first = {row["key"]: row for row in started["subtasks"]}
    before = _execution_count(client)
    assert _recover(client) >= 1
    finished = client.get(f"/v1/goals/{started['id']}", headers=auth()).json()
    again = {row["key"]: row for row in finished["subtasks"]}
    assert finished["status"] == "completed"
    assert again["s0"]["execution_id"] == first["s0"]["execution_id"]
    assert again["s0"]["attempt_count"] == 1
    assert _execution_count(client) == before + 1


def test_a_live_lease_blocks_a_second_worker(client: TestClient) -> None:
    started = _goal(client, SEQUENTIAL, max_waves=1)
    _hold_lease(client, started["id"], hours=1)
    assert _recover(client) == 0
    waiting = client.get(f"/v1/goals/{started['id']}", headers=auth()).json()
    assert waiting["status"] == "running"
    assert {row["key"]: row["status"] for row in waiting["subtasks"]}["s1"] == "pending"
    _hold_lease(client, started["id"], hours=-1)
    assert _recover(client) >= 1
    finished = client.get(f"/v1/goals/{started['id']}", headers=auth()).json()
    assert finished["status"] == "completed"


def test_two_claims_cannot_own_one_goal(client: TestClient) -> None:
    started = _goal(client, SEQUENTIAL, max_waves=0)
    now = client.app.state.clock.now()
    with client.app.state.session_factory() as session:
        assert claim_goal(session, started["id"], "worker-a", now, 60, 3) is True
        assert claim_goal(session, started["id"], "worker-b", now, 60, 3) is False


def test_expired_recovery_budget_fails_closed(client: TestClient) -> None:
    started = _goal(client, SEQUENTIAL, max_waves=0)
    with client.app.state.session_factory() as session:
        goal = session.get(Goal, started["id"])
        assert goal is not None
        goal.lease_owner = "dead"
        goal.lease_expires_at = client.app.state.clock.now() - timedelta(seconds=5)
        goal.recovery_attempts = client.app.state.settings.max_goal_recoveries
        session.commit()
    _recover(client)
    body = client.get(f"/v1/goals/{started['id']}", headers=auth()).json()
    assert body["status"] == "failed"
    assert body["error_code"] == "recovery_exhausted"
    assert all(row["execution_id"] is None for row in body["subtasks"])


def test_cancelled_and_terminal_goals_do_not_resume(client: TestClient) -> None:
    cancelled = _goal(client, SEQUENTIAL, max_waves=0)
    assert client.post(f"/v1/goals/{cancelled['id']}/cancel", headers=auth()).json()["status"] == (
        "cancelled"
    )
    again = client.post(f"/v1/goals/{cancelled['id']}/cancel", headers=auth()).json()
    assert again["status"] == "cancelled"
    _recover(client)
    assert (
        client.get(f"/v1/goals/{cancelled['id']}", headers=auth()).json()["status"] == "cancelled"
    )
    completed = _goal(client, PARALLEL)
    before = _execution_count(client)
    _recover(client)
    assert (
        client.get(f"/v1/goals/{completed['id']}", headers=auth()).json()["status"] == "completed"
    )
    assert _execution_count(client) == before


def test_stale_running_execution_is_not_repeated(client: TestClient) -> None:
    completed = _goal(client, PARALLEL)
    before = _execution_count(client)
    with client.app.state.session_factory() as session:
        goal = session.get(Goal, completed["id"])
        assert goal is not None
        goal.status = "running"
        subtask = session.scalar(
            select(GoalSubtask).where(
                GoalSubtask.goal_id == goal.id, GoalSubtask.subtask_key == "s0"
            )
        )
        assert subtask is not None and subtask.execution_id is not None
        subtask.status = "running"
        session.commit()
    _recover(client)
    body = client.get(f"/v1/goals/{completed['id']}", headers=auth()).json()
    assert body["status"] == "completed"
    assert _execution_count(client) == before


def test_stale_invocation_without_an_execution_can_retry_when_idempotent(
    client: TestClient,
) -> None:
    started = _goal(client, SEQUENTIAL, max_waves=1)
    with client.app.state.session_factory() as session:
        subtask = session.scalar(
            select(GoalSubtask).where(
                GoalSubtask.goal_id == started["id"], GoalSubtask.subtask_key == "s1"
            )
        )
        assert subtask is not None
        subtask.status = "running"
        subtask.attempt_count = 1
        session.commit()
    _recover(client)
    body = client.get(f"/v1/goals/{started['id']}", headers=auth()).json()
    again = {row["key"]: row for row in body["subtasks"]}
    assert body["status"] == "completed"
    assert again["s1"]["execution_id"] is not None


def test_uncertain_and_approval_goals_are_not_auto_resumed(client: TestClient) -> None:
    started = _goal(client, SEQUENTIAL, max_waves=1)
    with client.app.state.session_factory() as session:
        subtask = session.scalar(
            select(GoalSubtask).where(
                GoalSubtask.goal_id == started["id"], GoalSubtask.subtask_key == "s1"
            )
        )
        assert subtask is not None
        subtask.status = "uncertain"
        subtask.error_code = "outcome_uncertain"
        session.commit()
    before = _execution_count(client)
    _recover(client)
    uncertain = client.get(f"/v1/goals/{started['id']}", headers=auth()).json()
    assert uncertain["status"] == "uncertain"
    assert _execution_count(client) == before
    waiting = _goal(client, "Coordinate an export to finance-archive")
    assert waiting["status"] == "awaiting_approval"
    _recover(client)
    stayed = client.get(f"/v1/goals/{waiting['id']}", headers=auth()).json()
    assert stayed["status"] == "awaiting_approval"
    assert all(row["execution_id"] is None for row in stayed["subtasks"])


def test_plan_revision_keeps_completed_work_and_drops_pending_approval(
    client: TestClient,
) -> None:
    failed = _goal(client, FAIL)
    first = {row["key"]: row for row in failed["subtasks"]}
    with client.app.state.session_factory() as session:
        goal = session.get(Goal, failed["id"])
        subtask = session.scalar(
            select(GoalSubtask).where(
                GoalSubtask.goal_id == failed["id"],
                GoalSubtask.subtask_key == "s0",
            )
        )
        assert goal is not None and subtask is not None and subtask.plan_step_id is not None
        step = session.get(PlanStep, subtask.plan_step_id)
        assert step is not None
        session.add(
            Approval(
                id=new_id(),
                task_id=goal.task_id,
                plan_id=step.plan_id,
                plan_step_id=step.id,
                capability_id=step.capability_id,
                operation_hash="a" * 64,
                canonical_payload={"text": "stale"},
                canonical_bindings={},
                status="pending",
                requested_by_principal_id=goal.principal_id,
                expires_at=client.app.state.clock.now() + timedelta(hours=1),
                created_at=client.app.state.clock.now(),
            )
        )
        session.commit()
    revised = client.post(
        f"/v1/goals/{failed['id']}/replan", headers=auth(), params={"max_waves": 0}
    ).json()
    assert revised["status"] == "running"
    assert revised["revision"] == 2
    with client.app.state.session_factory() as session:
        approval = session.scalar(select(Approval).where(Approval.task_id == failed["task_id"]))
        assert approval is not None
        assert approval.status == "cancelled"
    _recover(client)
    finished = client.get(f"/v1/goals/{failed['id']}", headers=auth()).json()
    again = {row["key"]: row for row in finished["subtasks"]}
    assert finished["status"] == "partial"
    assert again["s0"]["execution_id"] == first["s0"]["execution_id"]
    assert finished["verifications"][-1]["revision"] == 2
    assert finished["verifications"][-1]["status"] == "passed"


def test_wrong_missing_and_stale_verification_cannot_complete(client: TestClient) -> None:
    completed = _goal(client, PARALLEL)
    with client.app.state.session_factory() as session:
        goal = session.get(Goal, completed["id"])
        assert goal is not None
        goal.status = "running"
        goal.revision = 9
        session.commit()
    _recover(client)
    stale = client.get(f"/v1/goals/{completed['id']}", headers=auth()).json()
    assert stale["status"] == "failed"
    assert stale["error_code"] == "verification_missing"
    broken = _goal(client, PARALLEL)
    with client.app.state.session_factory() as session:
        goal = session.get(Goal, broken["id"])
        subtask = session.scalar(
            select(GoalSubtask).where(
                GoalSubtask.goal_id == broken["id"], GoalSubtask.subtask_key == "s0"
            )
        )
        verifier = session.scalar(
            select(GoalSubtask).where(
                GoalSubtask.goal_id == broken["id"], GoalSubtask.agent_key == "verifier"
            )
        )
        assert goal is not None and subtask is not None and verifier is not None
        goal.status = "running"
        subtask.output_payload = {"word_count": 999}
        verifier.status = "pending"
        verifier.error_code = None
        session.commit()
    _recover(client)
    failed = client.get(f"/v1/goals/{broken['id']}", headers=auth()).json()
    assert failed["status"] == "failed"
    assert failed["error_code"] == "verification_failed"


def test_missing_output_is_inconclusive(client: TestClient) -> None:
    started = _goal(client, PARALLEL, max_waves=1)
    with client.app.state.session_factory() as session:
        subtask = session.scalar(
            select(GoalSubtask).where(
                GoalSubtask.goal_id == started["id"], GoalSubtask.subtask_key == "s0"
            )
        )
        assert subtask is not None
        subtask.output_payload = {}
        session.commit()
    _recover(client)
    body = client.get(f"/v1/goals/{started['id']}", headers=auth()).json()
    assert body["status"] == "failed"
    assert body["error_code"] == "verification_inconclusive"
    assert body["verifications"][0]["status"] == "inconclusive"


def test_claim_assessment_and_artifacts_do_not_invent_success() -> None:
    assert assess_claim(evidence_present=False, supported=True, shared_model=False) == (
        "inconclusive"
    )
    assert assess_claim(evidence_present=True, supported=None, shared_model=True) == (
        "partially_verified"
    )
    assert assess_claim(evidence_present=True, supported=False, shared_model=False) == "failed"
    assert assess_claim(evidence_present=True, supported=True, shared_model=False) == "passed"
    assert verify_artifact(exists=False, digest="a", expected_digest="a") == "failed"
    assert verify_artifact(exists=True, digest=None, expected_digest="a") == "inconclusive"
    assert verify_artifact(exists=True, digest="a", expected_digest="b") == "failed"
    assert verify_artifact(exists=True, digest="a", expected_digest="a") == "passed"
    missing = verify_execution(
        verification_kind="text.analyze",
        execution_status="succeeded",
        resolved_input={"text": "alpha"},
        output={"word_count": "nope"},
    )
    assert missing.status.value == "failed"


def test_database_outage_does_not_crash_the_worker(client: TestClient) -> None:
    class _Down:
        def __call__(self) -> Session:
            raise SQLAlchemyError("database unavailable")

    assert (
        recover_available(
            factory=_Down(),  # type: ignore[arg-type]
            settings=client.app.state.settings,
            registry=client.app.state.registry,
            provider=client.app.state.provider,
            clock=client.app.state.clock,
        )
        == 0
    )


def test_several_goals_resume_in_one_pass(client: TestClient) -> None:
    first = _goal(client, SEQUENTIAL, max_waves=0)
    second = _goal(client, PARALLEL, max_waves=0)
    _recover(client)
    assert client.get(f"/v1/goals/{first['id']}", headers=auth()).json()["status"] == "completed"
    assert client.get(f"/v1/goals/{second['id']}", headers=auth()).json()["status"] == "completed"


def test_file_and_vault_secrets_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "secrets"
    directory.mkdir()
    secret = directory / "MCP_TOKEN"
    secret.write_text("local-file-secret\n", encoding="utf-8")
    clock = {"now": 0.0}
    monkeypatch.setattr("apex.secrets.time.monotonic", lambda: clock["now"])
    provider = FileSecretProvider(str(directory), cache_seconds=30)
    assert provider.ready() is True
    assert provider.resolve("file:MCP_TOKEN") == "local-file-secret"
    secret.write_text("rotated-secret\n", encoding="utf-8")
    assert provider.resolve("file:MCP_TOKEN") == "local-file-secret"
    clock["now"] = 60.0
    assert provider.resolve("file:MCP_TOKEN") == "rotated-secret"
    with pytest.raises(DomainError):
        provider.resolve("file:OTHER")

    def _ok(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Vault-Token"] == "vault-test-token"
        return httpx.Response(200, json={"data": {"data": {"value": "from-vault"}}})

    monkeypatch.setenv("APEX_VAULT_TOKEN", "vault-test-token")
    vault = VaultSecretProvider(
        "https://vault.example",
        "APEX_VAULT_TOKEN",
        "secret",
        30,
        httpx.Client(transport=httpx.MockTransport(_ok)),
    )
    assert vault.resolve("vault:mcp") == "from-vault"

    def _down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("vault unavailable")

    outage = VaultSecretProvider(
        "https://vault.example",
        "APEX_VAULT_TOKEN",
        "secret",
        1,
        httpx.Client(transport=httpx.MockTransport(_down)),
    )
    with pytest.raises(DomainError) as denied:
        outage.resolve("vault:mcp")
    assert "from-vault" not in str(denied.value)
    refused = VaultSecretProvider("https://vault.example", "APEX_VAULT_TOKEN", "secret", 30)
    monkeypatch.delenv("APEX_VAULT_TOKEN")
    with pytest.raises(DomainError):
        refused.resolve("vault:other")


def test_production_accepts_file_secrets_and_rejects_env_fallback(tmp_path: Path) -> None:
    directory = tmp_path / "secrets"
    directory.mkdir()
    assert_runtime_is_allowed(
        _production(
            secret_provider="file", secret_dir=str(directory), require_external_secrets=True
        )
    )
    with pytest.raises(RuntimeError, match="secret"):
        assert_runtime_is_allowed(_production(require_external_secrets=True))
    with pytest.raises(RuntimeError, match="VAULT"):
        assert_runtime_is_allowed(
            _production(secret_provider="vault", require_external_secrets=True, vault_addr="")
        )


def test_oidc_discovery_is_pinned_to_the_configured_issuer() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_issuer="https://idp.example",
        auth_audience=AUDIENCE,
        auth_oidc_discovery=True,
        auth_jwks_allow_http_fixture=False,
    )

    def fetcher(url: str, _settings: Settings, **_kwargs: object) -> bytes:
        if url.endswith("/.well-known/openid-configuration"):
            return json.dumps(
                {"issuer": "https://idp.example", "jwks_uri": "https://idp.example/jwks"}
            ).encode()
        return b'{"keys":[]}'

    authenticator = TokenAuthenticator(settings, fetcher=fetcher)
    assert authenticator.configured() is True
    assert authenticator._settings.auth_jwks_url == "https://idp.example/jwks"
    pinned = settings.model_copy(update={"auth_jwks_url": "https://idp.example/pinned"})
    kept = TokenAuthenticator(pinned, fetcher=fetcher)
    assert kept._settings.auth_jwks_url == "https://idp.example/pinned"

    def mismatch(url: str, _settings: Settings, **_kwargs: object) -> bytes:
        return json.dumps(
            {"issuer": "https://evil.example", "jwks_uri": "https://evil.example/jwks"}
        ).encode()

    failed = TokenAuthenticator(settings, fetcher=mismatch)
    assert failed.configured() is False

    def attack(url: str, current: Settings, **kwargs: object) -> bytes:
        if url.endswith("openid-configuration"):
            return json.dumps(
                {
                    "issuer": current.auth_issuer,
                    "jwks_uri": "http://169.254.169.254/latest",
                }
            ).encode()
        return fetch_bounded(url, current, **kwargs)  # type: ignore[arg-type]

    with pytest.raises(DomainError):
        discover_jwks_uri(settings, attack)
    with pytest.raises(RuntimeError, match="HTTPS"):
        assert_runtime_is_allowed(
            _production(auth_issuer="http://idp.example", auth_oidc_discovery=True)
        )


def test_remote_client_does_not_follow_redirects() -> None:
    assert MCP_MAX_REDIRECTS == 0


def test_memory_purge_removes_content_and_blocks_reintroduction(client: TestClient) -> None:
    created = client.post(
        "/v1/memory",
        headers=auth(),
        json={
            "scope_type": "personal",
            "category": "fact",
            "content": "PURGE-TOKEN-77 should disappear",
            "source_type": "user",
            "source_id": "note",
            "confirmed": True,
        },
    )
    assert created.status_code == 200, created.text
    memory_id = created.json()["id"]
    other = client.post(
        "/v1/memory",
        headers=auth(),
        json={
            "scope_type": "project",
            "scope_id": "keep",
            "category": "fact",
            "content": "KEEP-TOKEN-12 stays",
            "source_type": "user",
            "source_id": "note",
            "confirmed": True,
        },
    )
    assert other.status_code == 200, other.text
    assert client.delete(f"/v1/memory/{memory_id}", headers=auth()).status_code == 200
    denied = client.post("/v1/memory/purge", headers=auth())
    assert denied.status_code == 403
    with client.app.state.session_factory() as session:
        record = session.get(MemoryRecord, memory_id)
        assert record is not None
        record.deleted_at = client.app.state.clock.now() - timedelta(hours=48)
        session.add(
            ContextRecord(
                id=new_id(),
                scope_type="task",
                scope_id="task-purge",
                principal_id=None,
                trust="trusted",
                kind="verified_outcome",
                content={"outputs": ["prefix PURGE-TOKEN-77 should disappear suffix"]},
                created_at=client.app.state.clock.now(),
            )
        )
        session.add(
            ContextRecord(
                id=new_id(),
                scope_type="task",
                scope_id="task-keep",
                principal_id=None,
                trust="proposal",
                kind="note",
                content={"outputs": ["KEEP-TOKEN-12 stays"]},
                created_at=client.app.state.clock.now(),
            )
        )
        session.commit()
    purged = client.post("/v1/memory/purge", headers=auth("dev-approver"))
    assert purged.status_code == 200, purged.text
    assert purged.json()["purged"] == 1
    assert client.get(f"/v1/memory/{memory_id}", headers=auth()).status_code == 404
    found = client.post("/v1/memory/search", headers=auth(), json={"text": "PURGE-TOKEN-77"})
    assert found.status_code == 200
    assert all("PURGE-TOKEN-77" not in row["content"] for row in found.json()["memories"])
    context = client.post(
        "/v1/memory/context",
        headers=auth(),
        json={"query": "PURGE-TOKEN-77"},
    )
    assert "PURGE-TOKEN-77" not in str(context.json())
    kept = client.get(f"/v1/memory/{other.json()['id']}", headers=auth())
    assert kept.status_code == 200
    assert "KEEP-TOKEN-12" in kept.json()["content"]
    with client.app.state.session_factory() as session:
        tombstone = session.scalar(select(MemoryPurge).where(MemoryPurge.memory_id == memory_id))
        assert tombstone is not None
        assert "content" not in MemoryPurge.__table__.columns
        assert session.get(MemoryRecord, memory_id) is None
        assert (
            session.scalar(select(MemoryRevision).where(MemoryRevision.memory_id == memory_id))
            is None
        )
        stored = list(session.scalars(select(ContextRecord)))
        rendered = str([row.content for row in stored])
        assert "PURGE-TOKEN-77" not in rendered
        assert "[redacted]" in rendered
        assert "KEEP-TOKEN-12" in rendered
    assert "content" not in {column.name for column in MemoryPurge.__table__.columns}


def test_ready_requires_a_current_schema(engine: object) -> None:
    application = create_app(
        settings=_production(
            auth_issuer=ISSUER,
            auth_audience=AUDIENCE,
            auth_public_key_pem=PUBLIC_PEM,
        ),
        engine=engine,  # type: ignore[arg-type]
    )
    with TestClient(application) as live:
        assert live.get("/health").status_code == 200
        assert live.get("/ready").status_code == 503


def _recover(client: TestClient) -> int:
    return recover_available(
        factory=client.app.state.session_factory,
        settings=client.app.state.settings,
        registry=client.app.state.registry,
        provider=client.app.state.provider,
        clock=client.app.state.clock,
    )


def _execution_count(client: TestClient) -> int:
    with client.app.state.session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(Execution)) or 0)


def _hold_lease(client: TestClient, goal_id: str, *, hours: int) -> None:
    with client.app.state.session_factory() as session:
        goal = session.get(Goal, goal_id)
        assert goal is not None
        goal.lease_owner = "other-worker"
        goal.lease_expires_at = client.app.state.clock.now() + timedelta(hours=hours)
        session.commit()


def _production(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "environment": "production",
        "dev_auth_enabled": False,
        "seed_dev_principals": False,
        "auth_private_key_pem": "",
        "dev_token_issuance": False,
        "database_url": "sqlite+pysqlite:///:memory:",
        "model_provider": "mock",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]
