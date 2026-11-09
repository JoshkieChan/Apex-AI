"""PostgreSQL purge removes memory text from rows that could reconstruct it."""

from __future__ import annotations

import hashlib
import os
from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from tests.conftest import auth
from tests.integration.postgres_db import disposable_database
from tests.support_auth import AUDIENCE, ISSUER, PRIVATE_PEM, PUBLIC_PEM

from apex.api.app import create_app
from apex.clock import SystemClock
from apex.config import Settings
from apex.memory.store import MemoryStore
from apex.persistence.models import (
    ContextRecord,
    MemoryEvent,
    MemoryPurge,
    MemoryRecord,
    MemoryRevision,
)
from apex.runtime_ids import new_id

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
TOKEN = "PURGE-TOKEN-77 should disappear"
pytestmark = pytest.mark.skipif(
    not os.environ.get("APEX_POSTGRES_TEST_URL"),
    reason="APEX_POSTGRES_TEST_URL is not set",
)


def test_purged_memory_cannot_be_reconstructed_after_reconnect() -> None:
    name = f"apex_purge_{os.getpid()}"
    with disposable_database(name) as url:
        os.environ["APEX_DATABASE_URL"] = url
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        application = create_app(
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
                instance_id="purge",
            )
        )
        try:
            with TestClient(application) as client:
                created = client.post(
                    "/v1/memory",
                    headers=auth(),
                    json={
                        "scope_type": "personal",
                        "category": "fact",
                        "content": TOKEN,
                        "source_type": "user",
                        "source_id": "note",
                        "confirmed": True,
                    },
                )
                assert created.status_code == 200, created.text
                memory_id = created.json()["id"]
                assert client.delete(f"/v1/memory/{memory_id}", headers=auth()).status_code == 200
                with application.state.session_factory() as session:
                    record = session.get(MemoryRecord, memory_id)
                    assert record is not None
                    record.deleted_at = application.state.clock.now() - timedelta(hours=48)
                    session.add(
                        ContextRecord(
                            id=new_id(),
                            scope_type="task",
                            scope_id="task-purge",
                            principal_id=None,
                            trust="trusted",
                            kind="verified_outcome",
                            content={"preview": f"before {TOKEN} after"},
                            created_at=application.state.clock.now(),
                        )
                    )
                    session.commit()
                purged = client.post("/v1/memory/purge", headers=auth("dev-approver"))
                assert purged.status_code == 200, purged.text
                found = client.post("/v1/memory/search", headers=auth(), json={"text": TOKEN})
                assert all(TOKEN not in row["content"] for row in found.json()["memories"])
                context = client.post("/v1/memory/context", headers=auth(), json={"query": TOKEN})
                assert TOKEN not in str(context.json())
        finally:
            application.state.engine.dispose()
        engine = create_engine(url)
        with Session(engine) as session:
            assert session.get(MemoryRecord, memory_id) is None
            assert (
                session.scalar(select(MemoryRevision).where(MemoryRevision.memory_id == memory_id))
                is None
            )
            tombstone = session.scalar(
                select(MemoryPurge).where(MemoryPurge.memory_id == memory_id)
            )
            assert tombstone is not None
            rendered = session.execute(text("SELECT content::text FROM context_records")).scalars()
            joined = " ".join(rendered)
            assert TOKEN not in joined
            assert "[redacted]" in joined
        engine.dispose()


def test_purge_keeps_operational_audit_and_redacts_only_the_documented_forms() -> None:
    short = "brief-note"
    name = f"apex_life_{os.getpid()}"
    with disposable_database(name) as url:
        os.environ["APEX_DATABASE_URL"] = url
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        application = create_app(
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
                instance_id="lifecycle",
                operational_retention_days=1,
            )
        )
        try:
            with TestClient(application) as client:
                personal = _memory(client, "personal", None, TOKEN)
                project = _memory(client, "project", "apollo", short)
                task = client.post(
                    "/v1/tasks",
                    headers=auth(),
                    json={
                        "request": f"Analyze the following text and report its word count: {TOKEN}"
                    },
                )
                assert task.status_code in {200, 201}, task.text
                export = client.post(
                    "/v1/tasks",
                    headers=auth(),
                    json={
                        "request": (
                            "Export a record to the archive labeled finance-archive "
                            'with content {"note":"quarterly"}'
                        )
                    },
                )
                assert export.status_code in {200, 201}, export.text
                goal = client.post(
                    "/v1/goals",
                    headers=auth(),
                    json={
                        "objective": (
                            "Coordinate sequentially: count the words in 'alpha beta' "
                            "then count the words in 'gamma'"
                        )
                    },
                    params={"max_waves": 0},
                )
                assert goal.status_code == 200, goal.text
                assert goal.json()["status"] == "running"
                assert client.delete(f"/v1/memory/{personal}", headers=auth()).status_code == 200
                assert client.delete(f"/v1/memory/{project}", headers=auth()).status_code == 200
                moment = application.state.clock.now()
                digest = hashlib.sha256(TOKEN.encode()).hexdigest()
                with application.state.session_factory() as session:
                    for memory_id in (personal, project):
                        record = session.get(MemoryRecord, memory_id)
                        assert record is not None
                        record.deleted_at = moment - timedelta(hours=48)
                    session.add(_row("exact", {"preview": f"before {TOKEN} after"}, moment))
                    session.add(_row("case", {"preview": TOKEN.upper()}, moment))
                    session.add(
                        _row("space", {"preview": "PURGE-TOKEN-77   should   disappear"}, moment)
                    )
                    session.add(_row("twice", {"a": TOKEN, "b": TOKEN}, moment))
                    session.add(_row("short", {"preview": short}, moment))
                    session.add(_row("digest", {"preview": digest}, moment))
                    session.add(_row("suffix", {"preview": "should disappear"}, moment))
                    session.add(
                        ContextRecord(
                            id=new_id(),
                            scope_type="task",
                            scope_id="old-proposal",
                            principal_id=None,
                            trust="proposal",
                            kind="note",
                            content={"text": "old proposal"},
                            created_at=moment - timedelta(days=3),
                        )
                    )
                    session.commit()
                    audit_before = _audit(session)
                purged = client.post("/v1/memory/purge", headers=auth("dev-approver"))
                assert purged.status_code == 200, purged.text
                assert purged.json()["purged"] == 2
                again = client.post("/v1/memory/purge", headers=auth("dev-approver"))
                assert again.json()["purged"] == 0
                found = client.post("/v1/memory/search", headers=auth(), json={"text": TOKEN})
                assert all(TOKEN not in row["content"] for row in found.json()["memories"])
                assembled = client.post("/v1/memory/context", headers=auth(), json={"query": TOKEN})
                assert TOKEN not in str(assembled.json())
                assert short not in str(assembled.json())
                reread = client.get(f"/v1/tasks/{task.json()['id']}", headers=auth())
                assert TOKEN in reread.json()["request"]
                still = client.get(f"/v1/goals/{goal.json()['id']}", headers=auth())
                assert still.json()["status"] == "running"
                assert "alpha beta" in still.json()["original_request"]
        finally:
            application.state.engine.dispose()
        engine = create_engine(url)
        with Session(engine) as session:
            assert session.get(MemoryRecord, personal) is None
            assert session.get(MemoryRecord, project) is None
            assert (
                session.scalar(select(MemoryRevision).where(MemoryRevision.memory_id == personal))
                is None
            )
            assert (
                session.scalar(select(MemoryEvent).where(MemoryEvent.memory_id == personal)) is None
            )
            tombstones = list(
                session.scalars(
                    select(MemoryPurge).where(MemoryPurge.memory_id.in_([personal, project]))
                )
            )
            assert len(tombstones) == 2
            assert "content" not in MemoryPurge.__table__.columns
            rendered = " ".join(
                session.execute(text("SELECT content::text FROM context_records")).scalars()
            )
            assert TOKEN not in rendered
            assert TOKEN.upper() not in rendered
            assert "PURGE-TOKEN-77   should" not in rendered
            assert "[redacted]" in rendered
            assert short in rendered
            assert digest in rendered
            assert "should disappear" in rendered
            assert "old proposal" in rendered
            requests = " ".join(session.execute(text("SELECT request_text FROM tasks")).scalars())
            assert TOKEN in requests
            assert _audit(session) == audit_before
            removed = MemoryStore(
                session,
                SystemClock(),
                Settings(_env_file=None, operational_retention_days=1),
            ).expire_operational_proposals(moment)
            session.commit()
            assert removed == 1
            again_removed = MemoryStore(
                session,
                SystemClock(),
                Settings(_env_file=None, operational_retention_days=1),
            ).expire_operational_proposals(moment)
            session.commit()
            assert again_removed == 0
            kept = " ".join(
                session.execute(text("SELECT content::text FROM context_records")).scalars()
            )
            assert "old proposal" not in kept
            assert short in kept
            assert _audit(session) == audit_before
        engine.dispose()


def _memory(client: TestClient, scope_type: str, scope_id: str | None, content: str) -> str:
    body: dict[str, object] = {
        "scope_type": scope_type,
        "category": "fact",
        "content": content,
        "source_type": "user",
        "source_id": "note",
        "confirmed": True,
    }
    if scope_id is not None:
        body["scope_id"] = scope_id
    created = client.post("/v1/memory", headers=auth(), json=body)
    assert created.status_code == 200, created.text
    memory_id = created.json()["id"]
    assert isinstance(memory_id, str)
    return memory_id


def _row(scope_id: str, content: dict[str, object], created_at: object) -> ContextRecord:
    return ContextRecord(
        id=new_id(),
        scope_type="task",
        scope_id=scope_id,
        principal_id=None,
        trust="trusted",
        kind="verified_outcome",
        content=content,
        created_at=created_at,
    )


def _audit(session: Session) -> dict[str, int]:
    statements = {
        "tasks": "SELECT COUNT(*) FROM tasks",
        "executions": "SELECT COUNT(*) FROM executions",
        "approvals": "SELECT COUNT(*) FROM approvals",
        "goals": "SELECT COUNT(*) FROM goals",
        "agent_invocations": "SELECT COUNT(*) FROM agent_invocations",
    }
    return {
        name: int(session.execute(text(statement)).scalar() or 0)
        for name, statement in statements.items()
    }
