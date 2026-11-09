"""Alembic upgrades, downgrades, and an optional PostgreSQL check."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from apex.persistence.base import Base

ROOT = Path(__file__).resolve().parents[2]


def test_sqlite_migration_matches_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_path = tmp_path / "apex.db"
    url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("APEX_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(url)
    inspector = inspect(engine)
    for table_name, table in Base.metadata.tables.items():
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        assert columns == set(table.columns.keys())
    engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("APEX_POSTGRES_TEST_URL"),
    reason="APEX_POSTGRES_TEST_URL is not set; SQLite migration coverage still runs.",
)
def test_postgres_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    url = os.environ["APEX_POSTGRES_TEST_URL"]
    monkeypatch.setenv("APEX_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    engine = create_engine(url)
    inspector = inspect(engine)
    names = inspector.get_table_names()
    assert "tasks" in names
    assert "approvals" in names
    assert "goals" in names
    assert "logical_agents" in names
    engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("APEX_POSTGRES_TEST_URL"),
    reason="APEX_POSTGRES_TEST_URL is not set; SQLite migration coverage still runs.",
)
def test_fresh_postgres_upgrades_downgrades_and_runs_a_goal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disposable database, not the developer's Compose volume."""
    from urllib.parse import urlsplit, urlunsplit

    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from tests.conftest import auth
    from tests.security.test_coordination import PARALLEL
    from tests.support_auth import AUDIENCE, ISSUER, PRIVATE_PEM, PUBLIC_PEM

    from apex.api.app import create_app
    from apex.config import Settings
    from apex.worker import recover_available

    source = os.environ["APEX_POSTGRES_TEST_URL"]
    parts = urlsplit(source)
    name = f"apex_m51_{os.getpid()}"
    admin_url = urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, ""))
    fresh_url = urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, ""))
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    admin.connect().execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    monkeypatch.setenv("APEX_DATABASE_URL", fresh_url)
    config = Config(str(ROOT / "alembic.ini"))
    try:
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        engine = create_engine(fresh_url)
        inspector = inspect(engine)
        names = set(inspector.get_table_names())
        assert {"goals", "goal_verifications", "memory_purges", "alembic_version"} <= names
        goal_columns = {column["name"] for column in inspector.get_columns("goals")}
        assert {"lease_owner", "lease_expires_at", "recovery_attempts"} <= goal_columns
        version = engine.connect().execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version == "0006_reliability"
        engine.dispose()
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
                database_url=fresh_url,
                model_provider="mock",
                instance_id="fresh",
            )
        )
        with TestClient(application) as client:
            started = client.post(
                "/v1/goals",
                headers=auth(),
                json={"objective": PARALLEL},
                params={"max_waves": 0},
            )
            assert started.status_code == 200, started.text
            assert started.json()["status"] == "running"
            recover_available(
                factory=application.state.session_factory,
                settings=application.state.settings,
                registry=application.state.registry,
                provider=application.state.provider,
                clock=application.state.clock,
            )
            finished = client.get(f"/v1/goals/{started.json()['id']}", headers=auth())
            assert finished.status_code == 200
            assert finished.json()["status"] == "completed"
            assert finished.json()["verifications"][0]["status"] == "passed"
        application.state.engine.dispose()
    finally:
        closer = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        connection = closer.connect()
        connection.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": name},
        )
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        connection.close()
        closer.dispose()
