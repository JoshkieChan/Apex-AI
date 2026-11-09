"""Real operating-system processes coordinating through PostgreSQL goal leases."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from tests.conftest import auth
from tests.integration.postgres_db import disposable_database
from tests.security.test_coordination import EXPORT, SEQUENTIAL
from tests.support_auth import AUDIENCE, ISSUER, PRIVATE_PEM, PUBLIC_PEM

from apex.api.app import create_app
from apex.config import Settings
from apex.persistence.models import Execution, Goal, GoalSubtask

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    not os.environ.get("APEX_POSTGRES_TEST_URL"),
    reason="APEX_POSTGRES_TEST_URL is not set",
)


def test_separate_processes_claim_recover_and_do_not_duplicate_work(tmp_path: Path) -> None:
    name = f"apex_mp_{os.getpid()}"
    with disposable_database(name) as url:
        _migrate(url)
        application = _app(url)
        try:
            with TestClient(application) as client:
                _prepare_barrier(url)
                goal = _goal(client, SEQUENTIAL, 0)
                _race(url, goal["id"])
                _clear_lease(url, goal["id"])
                hold = tmp_path / "hold-after-commit"
                winner = _spawn(
                    "advance", url, "worker-a", goal["id"], hold="APEX_TEST_HOLD_LEASE", path=hold
                )
                _wait_held(hold)
                before = _execution_ids(url, goal["id"])
                assert before["s0"] is not None
                assert before["s1"] is None
                loser = _spawn("advance", url, "worker-b", goal["id"])
                assert loser.wait(timeout=20) == 0
                assert _execution_ids(url, goal["id"]) == before
                winner.kill()
                assert winner.wait(timeout=10) is not None
                _expire_lease(url, goal["id"])
                bare = _goal(client, SEQUENTIAL, 0)
                before_exec = _spawn(
                    "advance",
                    url,
                    "worker-c",
                    bare["id"],
                    hold="APEX_TEST_HOLD_BEFORE_EXECUTION",
                    path=tmp_path / "hold-before",
                )
                _wait_held(tmp_path / "hold-before")
                assert _execution_ids(url, bare["id"])["s0"] is None
                assert _subtask_status(url, bare["id"], "s0") == "running"
                before_exec.kill()
                assert before_exec.wait(timeout=10) is not None
                _expire_lease(url, bare["id"])
                approval = _goal(client, EXPORT, None)
                cancelled = _goal(client, SEQUENTIAL, 0)
                completed = _goal(client, SEQUENTIAL, 0)
                uncertain = _goal(client, SEQUENTIAL, 0)
                exhausted = _goal(client, SEQUENTIAL, 0)
                _mark(url, cancelled["id"], "cancelled", attempts=0, expired=False)
                _mark(url, completed["id"], "completed", attempts=0, expired=False)
                _mark(url, uncertain["id"], "uncertain", attempts=0, expired=True)
                _mark(url, exhausted["id"], "running", attempts=3, expired=True)
                with url_session(url) as session:
                    subtask = session.scalar(
                        select(GoalSubtask).where(
                            GoalSubtask.goal_id == uncertain["id"],
                            GoalSubtask.subtask_key == "s1",
                        )
                    )
                    assert subtask is not None
                    subtask.status = "uncertain"
                    subtask.error_code = "outcome_uncertain"
                    session.commit()
                counts = _counts(url)
                replacement = _spawn("recover", url, "worker-d")
                assert replacement.wait(timeout=30) == 0
                resumed = _execution_ids(url, goal["id"])
                assert resumed["s0"] == before["s0"]
                assert resumed["s1"] is not None
                retried = _execution_ids(url, bare["id"])
                assert retried["s0"] is not None
                assert _status(url, approval["id"]) == "awaiting_approval"
                assert _status(url, cancelled["id"]) == "cancelled"
                assert _status(url, completed["id"]) == "completed"
                assert _status(url, uncertain["id"]) == "uncertain"
                assert _status(url, exhausted["id"]) == "failed"
                assert _counts(url)["executions"] >= counts["executions"]
                assert _execution_count_for(url, goal["id"], "s0") == 1
                assert _execution_count_for(url, bare["id"], "s0") == 1
        finally:
            application.state.engine.dispose()


def _migrate(url: str) -> None:
    os.environ["APEX_DATABASE_URL"] = url
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")


def _app(url: str) -> object:
    return create_app(
        settings=Settings(
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
            instance_id="parent",
        )
    )


def _prepare_barrier(url: str) -> None:
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE process_barrier (name text NOT NULL, owner text NOT NULL)")
        )
        connection.execute(
            text("CREATE TABLE process_result (owner text PRIMARY KEY, claimed boolean NOT NULL)")
        )
    engine.dispose()


def _goal(client: TestClient, objective: str, waves: int | None) -> dict[str, object]:
    params = {} if waves is None else {"max_waves": waves}
    response = client.post(
        "/v1/goals", headers=auth(), json={"objective": objective}, params=params
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _race(url: str, goal_id: object) -> None:
    first = _spawn("claim", url, "owner-a", str(goal_id), "2")
    second = _spawn("claim", url, "owner-b", str(goal_id), "2")
    assert first.wait(timeout=20) == 0
    assert second.wait(timeout=20) == 0
    engine = create_engine(url)
    with engine.connect() as connection:
        rows = list(connection.execute(text("SELECT owner, claimed FROM process_result")))
    engine.dispose()
    claimed = [row.owner for row in rows if row.claimed]
    assert claimed == ["owner-a"] or claimed == ["owner-b"]
    assert len(claimed) == 1


def _spawn(
    mode: str,
    url: str,
    owner: str,
    goal_id: str = "",
    extra: str = "",
    *,
    hold: str = "",
    path: Path | None = None,
) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env.pop("APEX_TEST_HOLD_LEASE", None)
    env.pop("APEX_TEST_HOLD_BEFORE_EXECUTION", None)
    if hold and path is not None:
        env[hold] = str(path)
    argv = [sys.executable, "-m", "tests.integration.process_child", mode, url, owner]
    if mode == "claim":
        argv.extend([goal_id, extra])
    elif mode == "advance":
        argv.append(goal_id)
    return subprocess.Popen(argv, cwd=str(ROOT), env=env)  # noqa: S603


def _wait_held(path: Path) -> None:
    deadline = __import__("time").monotonic() + 20
    while __import__("time").monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8") == "held":
            return
        __import__("time").sleep(0.05)
    raise AssertionError(f"process did not reach {path}")


def _clear_lease(url: str, goal_id: object) -> None:
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE goals SET lease_owner = NULL, lease_expires_at = NULL WHERE id = :id"),
            {"id": str(goal_id)},
        )
    engine.dispose()


def _expire_lease(url: str, goal_id: object) -> None:
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE goals SET lease_expires_at = NOW() - INTERVAL '1 second' WHERE id = :id"),
            {"id": str(goal_id)},
        )
    engine.dispose()


def url_session(url: str) -> Session:
    engine = create_engine(url)
    return Session(engine)


def _execution_ids(url: str, goal_id: object) -> dict[str, str | None]:
    with Session(create_engine(url)) as session:
        rows = session.scalars(select(GoalSubtask).where(GoalSubtask.goal_id == str(goal_id)))
        return {row.subtask_key: row.execution_id for row in rows}


def _execution_count_for(url: str, goal_id: object, key: str) -> int:
    with Session(create_engine(url)) as session:
        subtask = session.scalar(
            select(GoalSubtask).where(
                GoalSubtask.goal_id == str(goal_id), GoalSubtask.subtask_key == key
            )
        )
        assert subtask is not None and subtask.execution_id is not None
        return int(
            session.scalar(
                select(func.count())
                .select_from(Execution)
                .where(Execution.id == subtask.execution_id)
            )
            or 0
        )


def _subtask_status(url: str, goal_id: object, key: str) -> str:
    with Session(create_engine(url)) as session:
        subtask = session.scalar(
            select(GoalSubtask).where(
                GoalSubtask.goal_id == str(goal_id), GoalSubtask.subtask_key == key
            )
        )
        assert subtask is not None
        return subtask.status


def _status(url: str, goal_id: object) -> str:
    with Session(create_engine(url)) as session:
        goal = session.get(Goal, str(goal_id))
        assert goal is not None
        return goal.status


def _mark(url: str, goal_id: object, status: str, *, attempts: int, expired: bool) -> None:
    statement = (
        "UPDATE goals SET status = :status, recovery_attempts = :attempts, "
        "lease_owner = 'stale', lease_expires_at = NOW() - INTERVAL '1 second' "
        "WHERE id = :id"
    )
    if not expired:
        statement = (
            "UPDATE goals SET status = :status, recovery_attempts = :attempts, "
            "lease_owner = 'stale', lease_expires_at = NULL WHERE id = :id"
        )
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(statement),
            {"status": status, "attempts": attempts, "id": str(goal_id)},
        )
    engine.dispose()


def _counts(url: str) -> dict[str, int]:
    with Session(create_engine(url)) as session:
        total = int(session.scalar(select(func.count()).select_from(Execution)) or 0)
    return {"executions": total, "s0-dup": 1}
