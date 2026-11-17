"""The execution hold and a new attempt share one PostgreSQL row lock."""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from tests.integration.postgres_db import disposable_database

from apex.operations.controls import (
    claim_execution_slot,
    execution_suspended,
    set_execution_suspended,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    not os.environ.get("APEX_POSTGRES_TEST_URL"),
    reason="APEX_POSTGRES_TEST_URL is not set; the hold lock requires PostgreSQL.",
)
def test_hold_waits_until_the_open_attempt_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    name = f"apex_m8_hold_{os.getpid()}"
    with disposable_database(name) as url:
        monkeypatch.setenv("APEX_DATABASE_URL", url)
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        engine = create_engine(url)
        factory = sessionmaker(engine)
        locked = threading.Event()
        release = threading.Event()
        result: dict[str, float] = {}

        def reserve() -> None:
            session = factory()
            try:
                assert claim_execution_slot(session) is True
                locked.set()
                assert release.wait(15)
                session.commit()
            finally:
                session.close()

        def hold() -> None:
            assert locked.wait(15)
            session = factory()
            started = time.monotonic()
            try:
                set_execution_suspended(
                    session,
                    enabled=True,
                    actor_id="operator",
                    now=datetime.now(UTC),
                )
            finally:
                result["waited"] = time.monotonic() - started
                session.close()

        first = threading.Thread(target=reserve)
        second = threading.Thread(target=hold)
        first.start()
        second.start()
        assert locked.wait(15)
        time.sleep(0.5)
        with factory() as session:
            assert execution_suspended(session) is False
            assert _execution_count(session) == 0
        release.set()
        first.join(15)
        second.join(15)
        assert result["waited"] >= 0.4
        with factory() as session:
            assert execution_suspended(session) is True
            assert claim_execution_slot(session) is False
            assert _execution_count(session) == 0
            session.rollback()
        engine.dispose()


def _execution_count(session: Session) -> int:
    return int(session.scalar(text("SELECT COUNT(*) FROM executions")) or 0)
