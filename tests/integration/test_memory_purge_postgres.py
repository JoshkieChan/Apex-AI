"""PostgreSQL purge removes memory text from rows that could reconstruct it."""

from __future__ import annotations

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
from apex.config import Settings
from apex.persistence.models import ContextRecord, MemoryPurge, MemoryRecord, MemoryRevision
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
