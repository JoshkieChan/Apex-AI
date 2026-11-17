"""Shared fixtures. Tests use SQLite and the mock provider, never paid APIs."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from tests.support_auth import AUDIENCE, ISSUER, PRIVATE_PEM, PUBLIC_PEM, bearer

from apex.api.app import create_app
from apex.config import Settings
from apex.connectors.gates import reset_gates
from apex.connectors.reference import reset_reference
from apex.mcp.fixture import reset_fixture
from apex.persistence.base import Base
from apex.persistence.session import create_db_engine, create_session_factory

ANALYZE = "Analyze the following text and report its word count: The quick brown fox jumps"
EXPORT = 'Export a record to the archive labeled finance-archive with content {"note":"quarterly"}'
COMBINED = (
    "Count the words in 'alpha beta' and export the analysis to the archive labeled team-archive"
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        dev_auth_enabled=True,
        auth_issuer=ISSUER,
        auth_audience=AUDIENCE,
        auth_public_key_pem=PUBLIC_PEM,
        auth_private_key_pem=PRIVATE_PEM,
        seed_dev_principals=True,
        database_url="sqlite+pysqlite:///:memory:",
        model_provider="mock",
        log_level="WARNING",
        instance_id="test",
        lease_seconds=60,
        approval_ttl_hours=24,
        max_task_attempts=3,
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = create_session_factory(engine)
    db = factory()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def client(settings: Settings, engine: Engine) -> Iterator[TestClient]:
    application = create_app(settings=settings, engine=engine)
    with TestClient(application) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _fresh_fixture() -> Iterator[None]:
    reset_fixture()
    reset_reference()
    reset_gates()
    yield
    reset_fixture()
    reset_reference()
    reset_gates()


def auth(principal: str = "dev-user", idempotency_key: str | None = None) -> dict[str, str]:
    return bearer(principal, idempotency_key)
