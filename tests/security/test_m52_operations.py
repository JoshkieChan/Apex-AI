"""Worker deployment behavior and the memory retention boundaries."""

from __future__ import annotations

import inspect
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
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
from apex.worker import RecoveryReport, probe_database, recover_available, serve
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
    folded = "straße secret value"
    changed, matched = _redact_text("prefix STRASSE SECRET VALUE suffix", folded)
    assert matched
    assert changed == "prefix [redacted] suffix"
    ligature = "o\ufb03ce secret value"
    changed, matched = _redact_text("see OFFICE SECRET VALUE now", ligature)
    assert matched
    assert changed == "see [redacted] now"
    expanded = "\ufb03" * 4
    changed, matched = _redact_text(f"AA{expanded}BB", expanded)
    assert matched
    assert changed == "AA[redacted]BB"
    kept, matched = _redact_text("see STRASSE now", "straße")
    assert not matched
    assert kept == "see STRASSE now"
    spaced, matched = _redact_text("STRASSE   SECRET   VALUE", folded)
    assert matched
    assert "STRASSE" not in spaced


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
            report = recover_available(
                factory=application.state.session_factory,
                settings=application.state.settings,
                registry=application.state.registry,
                provider=application.state.provider,
                clock=application.state.clock,
            )
            assert report.goals_succeeded == 1
            assert report.goals_claimed == 1
            assert report.tasks_handled == 0
            finished = client.get(f"/v1/goals/{started.json()['id']}", headers=auth())
            assert finished.json()["status"] == "completed"
            assert finished.json()["verifications"][0]["status"] == "passed"
            waiting = client.post("/v1/goals", headers=auth(), json={"objective": EXPORT})
            assert waiting.status_code == 200, waiting.text
            held_report = recover_available(
                factory=application.state.session_factory,
                settings=application.state.settings,
                registry=application.state.registry,
                provider=application.state.provider,
                clock=application.state.clock,
            )
            assert held_report.goals_succeeded == 0
            assert held_report.goals_deferred == 1
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
        worker_shutdown_seconds=1,
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
        status.mark_operational(when=datetime.now(UTC), eligible=0, goals_claimed=0)
        status.mark_shutting_down()
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health")
            raise AssertionError("shutting down worker reported healthy")
        except urllib.error.HTTPError as error:
            assert error.code == 503
    finally:
        server.shutdown()
        server.server_close()


def test_claimed_work_is_not_counted_as_a_successful_recovery() -> None:
    status = WorkerStatus("metrics")
    status.mark_operational(
        when=datetime.now(UTC),
        eligible=4,
        tasks_handled=2,
        tasks_succeeded=1,
        tasks_failed=1,
        goals_claimed=3,
        goals_succeeded=0,
        goals_failed=1,
        goals_uncertain=1,
        goals_deferred=1,
    )
    body = status.snapshot()
    assert body["claimed_goals"] == 3
    assert body["tasks_handled"] == 2
    assert body["recovery_successes"] == 1
    assert body["recovery_failures"] == 2
    assert body["goals_uncertain"] == 1
    assert body["goals_deferred"] == 1
    assert "password" not in json.dumps(body)
    assert "postgresql" not in json.dumps(body)


def test_a_stale_poll_is_unhealthy_until_the_next_success() -> None:
    status = WorkerStatus("stale", freshness_seconds=0.2)
    server, port = start_health_server(0, status)
    try:
        status.mark_operational(when=datetime.now(UTC), eligible=0)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as response:
            assert response.status == 200
            assert json.loads(response.read())["poll_fresh"] is True
        time.sleep(0.35)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health")
            raise AssertionError("stale worker reported healthy")
        except urllib.error.HTTPError as error:
            assert error.code == 503
            stale = json.loads(error.read())
            assert stale["status"] == "degraded"
            assert stale["poll_fresh"] is False
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/live") as response:
            assert response.status == 200
        status.mark_operational(when=datetime.now(UTC), eligible=1, goals_succeeded=1)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as response:
            assert response.status == 200
            restored = json.loads(response.read())
            assert restored["status"] == "operational"
            assert restored["poll_fresh"] is True
            assert restored["recovery_successes"] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_shutdown_deadline_abandons_a_stuck_poll(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocked() -> RecoveryReport:
        started.set()
        release.wait(30)
        return RecoveryReport()

    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite+pysqlite:///{(tmp_path / 'shutdown.db').as_posix()}",
        model_provider="mock",
        worker_health_port=0,
        worker_interval_seconds=30,
        worker_shutdown_seconds=1,
        log_level="WARNING",
        instance_id="shutdown-worker",
    )
    stop = threading.Event()
    thread = threading.Thread(
        target=serve,
        args=(settings, stop),
        kwargs={"poll_once": blocked},
        daemon=True,
    )
    thread.start()
    assert started.wait(5)
    stop.set()
    thread.join(4)
    release.set()
    assert not thread.is_alive()


def test_a_goal_recovery_exception_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
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
            assert started.status_code == 200

            def boom(_self: object, _goal_id: str) -> str:
                raise RuntimeError("goal-recovery-failed")

            monkeypatch.setattr("apex.worker.CoordinationService.recover", boom)
            report = recover_available(
                factory=application.state.session_factory,
                settings=application.state.settings,
                registry=application.state.registry,
                provider=application.state.provider,
                clock=application.state.clock,
            )
            assert report.goals_failed == 1
            assert report.goals_succeeded == 0
            assert report.goals_claimed == 1
    finally:
        engine.dispose()


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
