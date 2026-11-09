"""Worker deployment behavior and the memory retention boundaries."""

from __future__ import annotations

import inspect
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.conftest import auth
from tests.security.test_coordination import EXPORT, SEQUENTIAL

from apex.api.app import create_app
from apex.clock import SystemClock
from apex.config import Settings, _apply_key_files, assert_runtime_is_allowed
from apex.logging_config import JsonFormatter
from apex.memory.store import MemoryStore, _redact_text
from apex.persistence.base import Base
from apex.persistence.models import ContextRecord, Execution
from apex.persistence.session import create_db_engine, create_session_factory
from apex.runtime_ids import new_id
from apex.worker import probe_database, recover_available, serve
from apex.worker_status import WorkerStatus, start_health_server

TOKEN = "PURGE-TOKEN-77 should disappear"


def test_redaction_matches_case_and_space_and_stops_at_short_text() -> None:
    changed, matched = _redact_text(f"keep {TOKEN.upper()} end", TOKEN)
    assert matched
    assert "PURGE-TOKEN-77" not in changed
    assert changed.startswith("keep ")
    spaced, matched = _redact_text("PURGE-TOKEN-77   should   disappear", TOKEN)
    assert matched
    assert "PURGE-TOKEN-77" not in spaced
    suffix, matched = _redact_text("should disappear", TOKEN)
    assert not matched
    assert suffix == "should disappear"
    short, matched = _redact_text("keep brief-note here", "brief-note")
    assert not matched
    assert "brief-note" in short
    digest = "a" * 64
    untouched, matched = _redact_text(digest, TOKEN)
    assert not matched
    assert untouched == digest


def test_old_context_proposals_expire_and_trusted_rows_stay() -> None:
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    moment = datetime.now(UTC)
    with factory() as session:
        session.add(_context("proposal", "proposal-body", moment - timedelta(days=10)))
        session.add(_context("trusted", "trusted-body", moment - timedelta(days=10)))
        session.commit()
        settings = Settings(_env_file=None, operational_retention_days=7)
        removed = MemoryStore(session, SystemClock(), settings).expire_operational_proposals(moment)
        session.commit()
        assert removed == 1
        remaining = list(session.scalars(select(ContextRecord)))
        assert [row.trust for row in remaining] == ["trusted"]
        assert remaining[0].content["text"] == "trusted-body"
        kept = MemoryStore(
            session, SystemClock(), Settings(_env_file=None, operational_retention_days=0)
        ).expire_operational_proposals(moment)
        assert kept == 0
        assert session.scalar(select(ContextRecord)) is not None
    engine.dispose()


def test_deferred_goal_is_finished_by_the_worker_without_run() -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        dev_auth_enabled=True,
        auth_issuer="https://apex.test/issuer",
        auth_audience="apex-api",
        auth_public_key_pem=_public(),
        auth_private_key_pem=_private(),
        seed_dev_principals=True,
        database_url="sqlite+pysqlite:///:memory:",
        model_provider="mock",
        defer_goal_execution=True,
        instance_id="api",
    )
    engine = create_db_engine(settings.database_url)
    Base.metadata.create_all(engine)
    application = create_app(settings=settings, engine=engine)
    try:
        with TestClient(application) as client:
            started = client.post("/v1/goals", headers=auth(), json={"objective": SEQUENTIAL})
            assert started.status_code == 200, started.text
            assert started.json()["status"] == "running"
            assert all(row["execution_id"] is None for row in started.json()["subtasks"])
            recover_available(
                factory=application.state.session_factory,
                settings=application.state.settings,
                registry=application.state.registry,
                provider=application.state.provider,
                clock=application.state.clock,
            )
            finished = client.get(f"/v1/goals/{started.json()['id']}", headers=auth())
            assert finished.json()["status"] == "completed"
            assert finished.json()["verifications"][0]["status"] == "passed"
            waiting = client.post("/v1/goals", headers=auth(), json={"objective": EXPORT})
            assert waiting.status_code == 200, waiting.text
            recover_available(
                factory=application.state.session_factory,
                settings=application.state.settings,
                registry=application.state.registry,
                provider=application.state.provider,
                clock=application.state.clock,
            )
            held = client.get(f"/v1/goals/{waiting.json()['id']}", headers=auth())
            assert held.json()["status"] == "awaiting_approval"
            with application.state.session_factory() as session:
                count = session.query(Execution).count()
                export_task = held.json()["task_id"]
                export_executions = (
                    session.query(Execution).filter(Execution.task_id == export_task).count()
                )
            assert count >= 1
            assert export_executions == 0
    finally:
        engine.dispose()


def test_worker_health_follows_the_database_poll(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{(tmp_path / 'worker.db').as_posix()}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=url,
        model_provider="mock",
        worker_health_port=0,
        worker_interval_seconds=0.05,
        log_level="WARNING",
        instance_id="unit-worker",
    )
    stop = threading.Event()
    found: list[WorkerStatus] = []
    thread = threading.Thread(
        target=serve, args=(settings, stop), kwargs={"status_out": found}, daemon=True
    )
    thread.start()
    try:
        port = _wait_port(found)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as response:
            assert response.status == 200
            assert b"operational" in response.read()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/live") as response:
            assert response.status == 200
    finally:
        stop.set()
        thread.join(5)
    assert not thread.is_alive()


def test_worker_health_is_not_healthy_before_a_poll() -> None:
    status = WorkerStatus("worker-test")
    server, port = start_health_server(0, status)
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health")
            raise AssertionError("starting worker reported healthy")
        except urllib.error.HTTPError as error:
            assert error.code == 503
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/live") as response:
            assert response.status == 200
        status.mark_degraded(when=datetime.now(UTC))
        status.mark_operational(when=datetime.now(UTC), eligible=0, claimed=0)
        status.mark_shutting_down()
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health")
            raise AssertionError("shutting down worker reported healthy")
        except urllib.error.HTTPError as error:
            assert error.code == 503
    finally:
        server.shutdown()
        server.server_close()


def test_database_exception_text_is_not_a_log_line() -> None:
    try:
        raise RuntimeError("postgresql://apex:super-secret-password@db/apex")
    except RuntimeError:
        record = logging.LogRecord("apex.worker", logging.ERROR, __file__, 1, "down", (), None)
        record.exc_info = __import__("sys").exc_info()
    line = JsonFormatter().format(record)
    assert "super-secret-password" not in line
    assert "RuntimeError" in line


def test_unreachable_database_is_not_a_successful_poll() -> None:
    factory = create_session_factory(
        create_db_engine(
            "postgresql+psycopg://apex:super-secret-password@127.0.0.1:1/apex?connect_timeout=1"
        )
    )
    assert probe_database(factory) is False


def test_a_missing_key_file_does_not_echo_a_path(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, auth_private_key_file=str(tmp_path / "missing.pem"))
    try:
        _apply_key_files(settings)
    except RuntimeError as error:
        assert "missing.pem" not in str(error)
    else:
        raise AssertionError("missing key file was accepted")


def test_production_refuses_a_private_key_file(tmp_path: Path) -> None:
    key = tmp_path / "private.pem"
    key.write_text("secret-key-material", encoding="utf-8")
    settings = _apply_key_files(
        Settings(
            _env_file=None,
            environment="production",
            auth_private_key_file=str(key),
        )
    )
    assert settings.auth_private_key_pem == "secret-key-material"
    try:
        assert_runtime_is_allowed(settings)
    except RuntimeError as error:
        assert "secret-key-material" not in str(error)
    else:
        raise AssertionError("production accepted a private key file")


def test_worker_source_does_not_open_its_own_mcp_client() -> None:
    import apex.worker as worker

    source = inspect.getsource(worker)
    assert "streamable_http" not in source
    assert "AsyncClient" not in source
    assert "httpx" not in source


def _context(trust: str, text: str, created_at: datetime) -> ContextRecord:
    return ContextRecord(
        id=new_id(),
        scope_type="task",
        scope_id="retention",
        principal_id=None,
        trust=trust,
        kind="note",
        content={"text": text},
        created_at=created_at,
    )


def _wait_port(found: list[WorkerStatus]) -> int:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if found and found[0].health_port and found[0].healthy():
            return found[0].health_port
        time.sleep(0.05)
    raise AssertionError("worker did not become operational")


def _public() -> str:
    from tests.support_auth import PUBLIC_PEM

    return PUBLIC_PEM


def _private() -> str:
    from tests.support_auth import PRIVATE_PEM

    return PRIVATE_PEM
