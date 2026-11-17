"""Process-boundary shutdown. The deadline ends the process; it does not cancel the adapter."""

from __future__ import annotations

import os
import signal
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
from tests.security.test_coordination import SEQUENTIAL
from tests.support_auth import AUDIENCE, ISSUER, PRIVATE_PEM, PUBLIC_PEM

from apex.api.app import create_app
from apex.config import Settings
from apex.persistence.models import Execution, Goal, GoalSubtask

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [
    pytest.mark.skipif(os.name == "nt", reason="SIGTERM process shutdown is verified on Linux"),
    pytest.mark.skipif(
        not os.environ.get("APEX_POSTGRES_TEST_URL"),
        reason="APEX_POSTGRES_TEST_URL is not set",
    ),
]


def test_sigterm_ends_the_process_and_does_not_replay_a_possible_write(tmp_path: Path) -> None:
    name = f"apex_stop_{os.getpid() % 100000}"
    with disposable_database(name) as url:
        _migrate(url)
        application = create_app(settings=_settings(url))
        try:
            with TestClient(application) as client:
                finished = client.post("/v1/goals", headers=auth(), json={"objective": SEQUENTIAL})
                assert finished.status_code == 200, finished.text
                finished_id = str(finished.json()["id"])
                committed = _execution_count(url, finished_id)
                assert committed >= 1
                started = client.post(
                    "/v1/goals",
                    headers=auth(),
                    params={"max_waves": 0},
                    json={"objective": SEQUENTIAL},
                )
                assert started.status_code == 200, started.text
                goal_id = str(started.json()["id"])
                hold = tmp_path / "adapter-hold"
                log = tmp_path / "worker.log"
                worker = _spawn(url, log, hold=hold, instance="shutdown-a")
                _wait_held(hold)
                worker.send_signal(signal.SIGTERM)
                timed_out = False
                try:
                    code = worker.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait(timeout=5)
                    timed_out = True
                    code = 1
                if timed_out:
                    raise AssertionError("worker process did not exit after the shutdown deadline")
                assert code == 0
                _assert_log_has_no_secret(log, url)
                _expire_lease(url, goal_id)
                follow = _spawn(url, tmp_path / "follow.log", instance="shutdown-b", once=True)
                assert follow.wait(timeout=30) == 0
                _assert_log_has_no_secret(tmp_path / "follow.log", url)
                execution = _step_execution(url, goal_id, "s0")
                assert execution is not None
                assert execution.status == "uncertain"
                assert execution.outcome_certainty == "possibly_sent"
                subtask = _subtask(url, goal_id, "s0")
                assert subtask.status == "uncertain"
                assert subtask.execution_id == execution.id
                assert _status(url, goal_id) == "uncertain"
                assert _execution_count(url, goal_id) == 1
                assert _execution_count(url, finished_id) == committed
                third = _spawn(url, tmp_path / "third.log", instance="shutdown-c", once=True)
                assert third.wait(timeout=30) == 0
                assert _execution_count(url, goal_id) == 1
                assert _execution_count(url, finished_id) == committed
                assert _status(url, goal_id) == "uncertain"
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
        instance_id="shutdown-parent",
    )


def _migrate(url: str) -> None:
    previous = os.environ.get("APEX_DATABASE_URL")
    os.environ["APEX_DATABASE_URL"] = url
    try:
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    finally:
        if previous is None:
            os.environ.pop("APEX_DATABASE_URL", None)
        else:
            os.environ["APEX_DATABASE_URL"] = previous


def _spawn(
    url: str,
    log: Path,
    *,
    instance: str,
    hold: Path | None = None,
    once: bool = False,
) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["APEX_DATABASE_URL"] = url
    env["APEX_ENVIRONMENT"] = "test"
    env["APEX_MODEL_PROVIDER"] = "mock"
    env["APEX_DEV_AUTH_ENABLED"] = "false"
    env["APEX_SEED_DEV_PRINCIPALS"] = "false"
    env["APEX_LOG_LEVEL"] = "WARNING"
    env["APEX_WORKER_HEALTH_PORT"] = "0"
    env["APEX_WORKER_SHUTDOWN_SECONDS"] = "1"
    env["APEX_WORKER_INTERVAL_SECONDS"] = "0.2"
    env["APEX_INSTANCE_ID"] = instance
    env.pop("APEX_TEST_HOLD_ADAPTER", None)
    env.pop("APEX_TEST_HOLD_LEASE", None)
    env.pop("APEX_TEST_HOLD_BEFORE_EXECUTION", None)
    if hold is not None:
        env["APEX_TEST_HOLD_ADAPTER"] = str(hold)
    argv = [sys.executable, "-m", "apex.worker"]
    if once:
        argv.append("--once")
    handle = log.open("wb")
    try:
        return subprocess.Popen(  # noqa: S603
            argv, cwd=str(ROOT), env=env, stdout=handle, stderr=subprocess.STDOUT
        )
    finally:
        handle.close()


def _wait_held(path: Path) -> None:
    import time

    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8") == "held":
            return
        time.sleep(0.05)
    raise AssertionError("worker did not enter the in-flight adapter")


def _expire_lease(url: str, goal_id: str) -> None:
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE goals SET lease_expires_at = NOW() - INTERVAL '1 second' WHERE id = :id"),
            {"id": goal_id},
        )
    engine.dispose()


def _execution_count(url: str, goal_id: str) -> int:
    with Session(create_engine(url)) as session:
        goal = session.get(Goal, goal_id)
        assert goal is not None
        return int(
            session.scalar(
                select(func.count()).select_from(Execution).where(Execution.task_id == goal.task_id)
            )
            or 0
        )


def _step_execution(url: str, goal_id: str, key: str) -> Execution | None:
    with Session(create_engine(url)) as session:
        subtask = session.scalar(
            select(GoalSubtask).where(
                GoalSubtask.goal_id == goal_id, GoalSubtask.subtask_key == key
            )
        )
        assert subtask is not None and subtask.plan_step_id
        row = session.scalar(
            select(Execution)
            .where(Execution.plan_step_id == subtask.plan_step_id)
            .order_by(Execution.attempt)
        )
        if row is not None:
            session.expunge(row)
        return row


def _subtask(url: str, goal_id: str, key: str) -> GoalSubtask:
    with Session(create_engine(url)) as session:
        subtask = session.scalar(
            select(GoalSubtask).where(
                GoalSubtask.goal_id == goal_id, GoalSubtask.subtask_key == key
            )
        )
        assert subtask is not None
        session.expunge(subtask)
        return subtask


def _status(url: str, goal_id: str) -> str:
    with Session(create_engine(url)) as session:
        goal = session.get(Goal, goal_id)
        assert goal is not None
        return goal.status


def _assert_log_has_no_secret(path: Path, url: str) -> None:
    text_log = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    assert "BEGIN PRIVATE" not in text_log
    assert url not in text_log
    assert "PRIVATE_PEM" not in text_log
