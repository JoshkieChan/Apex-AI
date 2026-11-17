"""A non-idempotent timeout stays uncertain through coordination."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.conftest import ANALYZE, auth
from tests.integration.postgres_db import disposable_database
from tests.security.test_coordination import SEQUENTIAL
from tests.support_auth import AUDIENCE, ISSUER, PRIVATE_PEM, PUBLIC_PEM

from apex.api.app import create_app
from apex.capabilities.builtins import build_default_registry
from apex.capabilities.registry import CapabilityRegistry
from apex.config import Settings
from apex.persistence.models import Execution, Goal, GoalSubtask
from apex.worker import recover_available

ROOT = Path(__file__).resolve().parents[2]
pytestmark_postgres = pytest.mark.skipif(
    not os.environ.get("APEX_POSTGRES_TEST_URL"),
    reason="APEX_POSTGRES_TEST_URL is not set",
)


class _RejectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    required_token: str


def test_non_idempotent_timeout_keeps_the_goal_uncertain(client: TestClient) -> None:
    _assert_timeout_path(client)


def test_pre_send_failure_is_not_uncertain(client: TestClient) -> None:
    _assert_pre_send_failure(client)


def test_idempotent_timeout_is_not_uncertain(client: TestClient) -> None:
    _assert_idempotent_timeout(client)


def test_task_timeout_follows_the_persisted_execution(client: TestClient) -> None:
    calls: list[int] = []
    _install(client.app.state.registry, calls, idempotent=False)
    created = client.post("/v1/tasks", headers=auth(), json={"request": ANALYZE})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "uncertain"
    assert body["error"]["code"] == "outcome_uncertain"
    assert len(calls) == 1
    execution = body["executions"][0]
    assert execution["status"] == "uncertain"
    assert execution["outcome_certainty"] == "possibly_sent"
    assert execution["error_code"] == "timed_out"


@pytestmark_postgres
def test_timeout_uncertainty_on_postgresql() -> None:
    name = f"apex_uncertain_{os.getpid()}"
    with disposable_database(name) as url:
        os.environ["APEX_DATABASE_URL"] = url
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        application = create_app(settings=_settings(url))
        try:
            with TestClient(application) as client:
                _assert_timeout_path(client)
                _assert_pre_send_failure(client)
                _assert_idempotent_timeout(client)
        finally:
            application.state.engine.dispose()


def _assert_timeout_path(client: TestClient) -> None:
    calls: list[int] = []
    _install(client.app.state.registry, calls, idempotent=False)
    started = client.post("/v1/goals", headers=auth(), json={"objective": SEQUENTIAL})
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["status"] == "uncertain"
    assert body["error_code"] == "outcome_uncertain"
    by_key = {row["key"]: row for row in body["subtasks"]}
    assert by_key["s0"]["status"] == "uncertain"
    assert by_key["s0"]["error_code"] == "outcome_uncertain"
    assert by_key["s0"]["execution_id"]
    assert len(calls) == 1
    with client.app.state.session_factory() as session:
        execution = session.get(Execution, by_key["s0"]["execution_id"])
        assert execution is not None
        assert execution.status == "uncertain"
        assert execution.outcome_certainty == "possibly_sent"
        assert execution.error_code == "timed_out"
        assert execution.evidence["raised_code"] == "timed_out"
        assert _execution_count(session, body["task_id"]) == 1
    report = recover_available(
        factory=client.app.state.session_factory,
        settings=client.app.state.settings,
        registry=client.app.state.registry,
        provider=client.app.state.provider,
        clock=client.app.state.clock,
    )
    assert report.goals_succeeded == 0
    assert len(calls) == 1
    resumed = client.post(f"/v1/goals/{body['id']}/run", headers=auth())
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "uncertain"
    assert len(calls) == 1
    revised = client.post(f"/v1/goals/{body['id']}/replan", headers=auth())
    assert revised.status_code == 409
    assert revised.json()["error"] == "outcome_uncertain"
    assert len(calls) == 1
    with client.app.state.session_factory() as session:
        goal = session.get(Goal, body["id"])
        assert goal is not None
        goal.status = "running"
        subtask = session.scalar(
            select(GoalSubtask).where(
                GoalSubtask.goal_id == body["id"],
                GoalSubtask.subtask_key == "s0",
            )
        )
        assert subtask is not None
        subtask.status = "pending"
        session.commit()
    forced = client.post(f"/v1/goals/{body['id']}/run", headers=auth())
    assert forced.status_code == 200, forced.text
    forced_body = forced.json()
    assert forced_body["status"] == "uncertain"
    again = {row["key"]: row for row in forced_body["subtasks"]}
    assert again["s0"]["execution_id"] == by_key["s0"]["execution_id"]
    assert len(calls) == 1
    with client.app.state.session_factory() as session:
        assert _execution_count(session, body["task_id"]) == 1


def _assert_pre_send_failure(client: TestClient) -> None:
    calls: list[int] = []
    registry: CapabilityRegistry = client.app.state.registry
    current = registry.get("text.analyze")
    assert current is not None

    def adapter(payload: Mapping[str, object], timeout_seconds: float) -> dict[str, object]:
        del payload, timeout_seconds
        calls.append(1)
        return {}

    registry.replace(replace(current, input_model=_RejectInput, schema_hash="", adapter=adapter))
    started = client.post("/v1/goals", headers=auth(), json={"objective": SEQUENTIAL})
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["status"] == "failed"
    assert body["status"] != "uncertain"
    by_key = {row["key"]: row for row in body["subtasks"]}
    assert by_key["s0"]["status"] == "failed"
    assert by_key["s0"]["error_code"] == "invalid_input"
    assert by_key["s0"]["execution_id"] is None
    assert calls == []
    with client.app.state.session_factory() as session:
        assert _execution_count(session, body["task_id"]) == 0


def _assert_idempotent_timeout(client: TestClient) -> None:
    calls: list[int] = []
    _install(client.app.state.registry, calls, idempotent=True)
    started = client.post("/v1/goals", headers=auth(), json={"objective": SEQUENTIAL})
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["status"] == "failed"
    assert body["error_code"] != "outcome_uncertain"
    by_key = {row["key"]: row for row in body["subtasks"]}
    assert by_key["s0"]["status"] == "failed"
    assert by_key["s0"]["execution_id"]
    with client.app.state.session_factory() as session:
        execution = session.get(Execution, by_key["s0"]["execution_id"])
        assert execution is not None
        assert execution.status == "timed_out"
        assert execution.outcome_certainty == "inconclusive"
        assert execution.status != "uncertain"


def _install(registry: CapabilityRegistry, calls: list[int], *, idempotent: bool) -> None:
    current = build_default_registry().get("text.analyze")
    assert current is not None

    def slow(payload: Mapping[str, object], timeout_seconds: float) -> dict[str, object]:
        del timeout_seconds
        calls.append(1)
        time.sleep(0.45)
        text = str(payload["text"])
        return {
            "word_count": len(text.split()),
            "character_count": len(text),
            "preview": text[:80],
        }

    registry.replace(
        replace(
            current,
            adapter=slow,
            idempotent=idempotent,
            timeout_seconds=0.15,
            max_attempts=1,
        )
    )


def _execution_count(session: Session, task_id: str) -> int:
    value = session.scalar(
        select(func.count()).select_from(Execution).where(Execution.task_id == task_id)
    )
    return int(value or 0)


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
        instance_id="uncertainty",
    )
