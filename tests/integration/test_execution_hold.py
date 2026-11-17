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
from apex.persistence.models import RuntimeControl

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


@pytest.mark.skipif(
    not os.environ.get("APEX_POSTGRES_TEST_URL"),
    reason="APEX_POSTGRES_TEST_URL is not set; the hold lock requires PostgreSQL.",
)
def test_same_session_sees_a_hold_committed_after_its_last_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A goal session commits between subtasks and keeps the old gate object."""
    name = f"apex_m8_hold_stale_{os.getpid()}"
    with disposable_database(name) as url:
        monkeypatch.setenv("APEX_DATABASE_URL", url)
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        engine = create_engine(url)
        factory = sessionmaker(engine, expire_on_commit=False, future=True)
        worker = factory()
        operator = factory()
        try:
            assert claim_execution_slot(worker) is True
            worker.commit()
            set_execution_suspended(
                operator,
                enabled=True,
                actor_id="operator",
                now=datetime.now(UTC),
            )
            assert claim_execution_slot(worker) is False
            worker.rollback()
        finally:
            worker.close()
            operator.close()
            engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("APEX_POSTGRES_TEST_URL"),
    reason="APEX_POSTGRES_TEST_URL is not set; the hold lock requires PostgreSQL.",
)
def test_unlocked_read_cannot_hide_a_hold_from_the_next_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An open session that already loaded the gate must not dispatch after the hold."""
    name = f"apex_m8_hold_read_{os.getpid()}"
    with disposable_database(name) as url:
        monkeypatch.setenv("APEX_DATABASE_URL", url)
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        engine = create_engine(url)
        factory = sessionmaker(engine, expire_on_commit=False, future=True)
        worker = factory()
        operator = factory()
        try:
            assert claim_execution_slot(worker) is True
            cached = worker.get(RuntimeControl, "execution_suspended")
            assert cached is not None
            worker.commit()
            assert execution_suspended(worker) is False
            set_execution_suspended(
                operator,
                enabled=True,
                actor_id="operator",
                now=datetime.now(UTC),
            )
            # This instance was loaded before the hold. Its attribute stays open.
            # The locked claim still has to refuse the new attempt.
            assert cached.enabled is False
            assert claim_execution_slot(worker) is False
            worker.rollback()
        finally:
            worker.close()
            operator.close()
            engine.dispose()


def _execution_count(session: Session) -> int:
    return int(session.scalar(text("SELECT COUNT(*) FROM executions")) or 0)
