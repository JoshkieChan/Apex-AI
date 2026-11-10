"""Initialize Apex on a PostgreSQL data directory that started empty.

Run this against the disposable Compose project in infrastructure/docker/compose.disposable.yml.
It refuses the development database name so it cannot be pointed at the existing volume by mistake.
"""

from __future__ import annotations

import os
import sys

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from tests.conftest import auth
from tests.security.test_coordination import SEQUENTIAL
from tests.support_auth import AUDIENCE, ISSUER, PRIVATE_PEM, PUBLIC_PEM

from apex.api.app import create_app
from apex.config import Settings
from apex.worker import recover_available

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def main() -> None:
    url = os.environ["APEX_DATABASE_URL"]
    if url.rstrip("/").endswith("/apex") and "55432" not in url and "@postgres:" not in url:
        raise SystemExit("refusing a database URL that is not the disposable endpoint")
    os.environ["APEX_DATABASE_URL"] = url
    config = Config(os.path.join(ROOT, "alembic.ini"))
    command.upgrade(config, "head")
    _assert_revision(url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    _assert_revision(url)
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
            instance_id="fresh-volume",
        )
    )
    try:
        with TestClient(application) as client:
            health = client.get("/health")
            assert health.status_code == 200, health.text
            assert health.json()["database"] == "ok"
            started = client.post(
                "/v1/goals",
                headers=auth(),
                json={"objective": SEQUENTIAL},
                params={"max_waves": 0},
            )
            assert started.status_code == 200, started.text
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
    finally:
        application.state.engine.dispose()
    production = create_app(
        settings=Settings(
            _env_file=None,
            environment="production",
            dev_auth_enabled=False,
            dev_token_issuance=False,
            seed_dev_principals=False,
            auth_issuer=ISSUER,
            auth_audience=AUDIENCE,
            auth_public_key_pem=PUBLIC_PEM,
            auth_private_key_pem="",
            database_url=url,
            model_provider="mock",
            secret_provider="env",
            require_external_secrets=False,
        )
    )
    try:
        with TestClient(production) as client:
            ready = client.get("/ready")
            assert ready.status_code == 200, ready.text
            assert ready.json()["status"] == "ready"
            health = client.get("/health")
            assert health.status_code == 200
            assert health.json()["schema_revision"] == "0008_connectors"
    finally:
        production.state.engine.dispose()
    print("fresh-volume-ok")


def _assert_revision(url: str) -> None:
    engine = create_engine(url)
    names = set(inspect(engine).get_table_names())
    assert {"goals", "goal_verifications", "memory_purges", "context_records"} <= names
    version = engine.connect().execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == "0008_connectors", version
    engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"fresh-volume-failed: {exc}", file=sys.stderr)
        raise
