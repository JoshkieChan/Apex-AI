"""PostgreSQL checks for Unicode redaction and proposal retention."""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.conftest import auth
from tests.integration.postgres_db import disposable_database
from tests.security.test_coordination import SEQUENTIAL
from tests.support_auth import AUDIENCE, ISSUER, PRIVATE_PEM, PUBLIC_PEM

from apex.api.app import create_app
from apex.clock import SystemClock
from apex.config import Settings
from apex.memory.store import MemoryStore
from apex.persistence.models import ContextRecord, Goal, MemoryRecord, Task
from apex.runtime_ids import new_id

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
FOLDED = "straße secret value"
LIGATURE = "o\ufb03ce secret value"
SHORT = "straße"
pytestmark = pytest.mark.skipif(
    not os.environ.get("APEX_POSTGRES_TEST_URL"),
    reason="APEX_POSTGRES_TEST_URL is not set",
)


def test_purge_redacts_casefold_expansions_at_the_original_span() -> None:
    name = f"apex_unicode_{os.getpid()}"
    with disposable_database(name) as url:
        os.environ["APEX_DATABASE_URL"] = url
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        application = create_app(settings=_settings(url))
        try:
            with TestClient(application) as client:
                folded = _memory(client, FOLDED)
                ligature = _memory(client, LIGATURE)
                short = _memory(client, SHORT)
                with application.state.session_factory() as session:
                    moment = application.state.clock.now()
                    for memory_id in (folded, ligature, short):
                        record = session.get(MemoryRecord, memory_id)
                        assert record is not None
                        record.deleted_at = moment - timedelta(hours=48)
                    session.add(_context("folded", {"text": f"prefix {FOLDED.upper()} suffix"}))
                    session.add(_context("ligature", {"text": "see OFFICE SECRET VALUE now"}))
                    session.add(_context("short", {"text": "see STRASSE now"}))
                    session.add(_context("around", {"text": "keep-this-neighbor"}))
                    session.commit()
                purged = client.post("/v1/memory/purge", headers=auth("dev-approver"))
                assert purged.status_code == 200, purged.text
                assert purged.json()["purged"] == 3
        finally:
            application.state.engine.dispose()
        from sqlalchemy import create_engine

        engine = create_engine(url)
        with Session(engine) as session:
            rows = {
                row.scope_id: row.content["text"] for row in session.scalars(select(ContextRecord))
            }
            assert rows["folded"] == "prefix [redacted] suffix"
            assert rows["ligature"] == "see [redacted] now"
            assert rows["short"] == "see STRASSE now"
            assert rows["around"] == "keep-this-neighbor"
        engine.dispose()


def test_old_proposals_for_unresolved_work_are_kept() -> None:
    name = f"apex_proposals_{os.getpid()}"
    with disposable_database(name) as url:
        os.environ["APEX_DATABASE_URL"] = url
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        application = create_app(settings=_settings(url))
        try:
            with TestClient(application) as client:
                running = _goal(client)
                waiting = _goal(client)
                uncertain = _goal(client)
                planned = _goal(client)
                completed = _goal(client)
                failed = _goal(client)
                task = client.post(
                    "/v1/tasks",
                    headers=auth(),
                    json={"request": "Analyze the following text and report its word count: hi"},
                )
                assert task.status_code == 201, task.text
                held_task = client.post(
                    "/v1/tasks",
                    headers=auth(),
                    json={"request": "Analyze the following text and report its word count: hold"},
                )
                assert held_task.status_code == 201, held_task.text
                with application.state.session_factory() as session:
                    moment = application.state.clock.now()
                    _status(session, waiting["id"], "awaiting_approval", "awaiting_approval")
                    _status(session, uncertain["id"], "uncertain", "completed")
                    _status(session, planned["id"], "planned", "planned")
                    _status(session, completed["id"], "completed", "completed")
                    _status(session, failed["id"], "failed", "failed")
                    approval_task = session.get(Task, held_task.json()["id"])
                    assert approval_task is not None
                    approval_task.status = "awaiting_approval"
                    uncertain_task = session.get(Task, task.json()["id"])
                    assert uncertain_task is not None
                    uncertain_task.status = "uncertain"
                    old = moment - timedelta(days=10)
                    kept = {
                        "running": running["task_id"],
                        "approval-goal": waiting["task_id"],
                        "uncertain-goal": uncertain["id"],
                        "planned": planned["task_id"],
                        "approval-task": held_task.json()["id"],
                        "uncertain-task": task.json()["id"],
                    }
                    expired = {
                        "completed": completed["task_id"],
                        "failed": failed["task_id"],
                        "orphan": "orphan-proposal",
                    }
                    for label, scope_id in {**kept, **expired}.items():
                        session.add(
                            ContextRecord(
                                id=new_id(),
                                scope_type="task",
                                scope_id=scope_id,
                                principal_id=None,
                                trust="proposal",
                                kind="note",
                                content={"text": label},
                                created_at=old,
                            )
                        )
                    session.add(
                        ContextRecord(
                            id=new_id(),
                            scope_type="task",
                            scope_id="trusted-row",
                            principal_id=None,
                            trust="trusted",
                            kind="note",
                            content={"text": "trusted-stays"},
                            created_at=old,
                        )
                    )
                    session.commit()
                    removed = MemoryStore(
                        session,
                        SystemClock(),
                        Settings(_env_file=None, operational_retention_days=7),
                    ).expire_operational_proposals(moment)
                    session.commit()
                    labels = {
                        str(row.content["text"])
                        for row in session.scalars(
                            select(ContextRecord).where(ContextRecord.trust == "proposal")
                        )
                    }
                    assert removed == len(expired)
                    assert labels == set(kept)
                    trusted = session.scalar(
                        select(ContextRecord).where(ContextRecord.scope_id == "trusted-row")
                    )
                    assert trusted is not None
                    assert trusted.content["text"] == "trusted-stays"
                    again = MemoryStore(
                        session,
                        SystemClock(),
                        Settings(_env_file=None, operational_retention_days=7),
                    ).expire_operational_proposals(moment)
                    session.commit()
                    assert again == 0
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
        instance_id="retention",
    )


def _memory(client: TestClient, content: str) -> str:
    created = client.post(
        "/v1/memory",
        headers=auth(),
        json={
            "scope_type": "personal",
            "category": "fact",
            "content": content,
            "source_type": "user",
            "source_id": "note",
            "confirmed": True,
        },
    )
    assert created.status_code == 200, created.text
    deleted = client.delete(f"/v1/memory/{created.json()['id']}", headers=auth())
    assert deleted.status_code == 200, deleted.text
    return str(created.json()["id"])


def _goal(client: TestClient) -> dict[str, object]:
    created = client.post(
        "/v1/goals",
        headers=auth(),
        json={"objective": SEQUENTIAL},
        params={"max_waves": 0},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "running"
    return body


def _status(session: Session, goal_id: str, status: str, task_status: str) -> None:
    goal = session.get(Goal, goal_id)
    assert goal is not None
    goal.status = status
    task = session.get(Task, goal.task_id)
    assert task is not None
    task.status = task_status


def _context(scope_id: str, content: dict[str, str]) -> ContextRecord:
    return ContextRecord(
        id=new_id(),
        scope_type="task",
        scope_id=scope_id,
        principal_id=None,
        trust="trusted",
        kind="note",
        content=content,
        created_at=SystemClock().now(),
    )
