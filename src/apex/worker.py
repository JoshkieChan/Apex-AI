"""Resume interrupted tasks and goals. PostgreSQL leases are the coordination authority."""

from __future__ import annotations

import argparse
import signal
import threading

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from apex.capabilities.builtins import build_default_registry
from apex.capabilities.registry import CapabilityRegistry
from apex.clock import Clock, SystemClock
from apex.config import Settings, assert_runtime_is_allowed, load_settings
from apex.coordination.leases import exhaust_goal_ids, recoverable_goal_ids
from apex.coordination.service import CoordinationService
from apex.logging_config import configure_logging, get_logger
from apex.memory.store import MemoryStore
from apex.persistence.repository import get_task, recoverable_task_ids
from apex.persistence.session import create_db_engine, create_session_factory
from apex.providers.base import ModelProvider
from apex.providers.factory import build_provider
from apex.runtime.factory import build_orchestrator
from apex.runtime.orchestrator import ready_after_approval_ids
from apex.worker_status import WorkerStatus, start_health_server

logger = get_logger("worker")


def recover_available(
    *,
    factory: sessionmaker[Session],
    settings: Settings,
    registry: CapabilityRegistry,
    provider: ModelProvider | None,
    clock: Clock,
) -> int:
    """Resume expired task leases, decided approvals, and recoverable goals."""
    try:
        handled = _recover_tasks(
            factory=factory,
            settings=settings,
            registry=registry,
            provider=provider,
            clock=clock,
        )
    except SQLAlchemyError:
        logger.exception("task recovery failed")
        return 0
    try:
        handled += _recover_goals(
            factory=factory, settings=settings, registry=registry, clock=clock
        )
        _purge_memory(factory=factory, settings=settings, clock=clock)
    except SQLAlchemyError:
        logger.exception("goal recovery failed")
    return handled


def _recover_tasks(
    *,
    factory: sessionmaker[Session],
    settings: Settings,
    registry: CapabilityRegistry,
    provider: ModelProvider | None,
    clock: Clock,
) -> int:
    with factory() as session:
        expired = recoverable_task_ids(session, clock.now())
        ready = ready_after_approval_ids(session)
    expired_ids = set(expired)
    task_ids = list(dict.fromkeys([*expired, *ready]))
    handled = 0
    for task_id in task_ids:
        with factory() as session:
            orchestrator = build_orchestrator(
                session=session,
                settings=settings,
                registry=registry,
                provider=provider,
                clock=clock,
            )
            task = get_task(session, task_id)
            if task is None:
                continue
            if task_id in expired_ids and task.attempt_count >= settings.max_task_attempts:
                orchestrator.fail_exhausted(task_id)
            else:
                if task_id in expired_ids:
                    task.attempt_count += 1
                    session.commit()
                orchestrator.advance(task_id)
            handled += 1
    return handled


def _recover_goals(
    *,
    factory: sessionmaker[Session],
    settings: Settings,
    registry: CapabilityRegistry,
    clock: Clock,
) -> int:
    with factory() as session:
        exhausted = exhaust_goal_ids(session, clock.now(), settings.max_goal_recoveries)
    for goal_id in exhausted:
        with factory() as session:
            CoordinationService(session, settings, registry, clock).exhaust(goal_id)
            session.commit()
    with factory() as session:
        goal_ids = recoverable_goal_ids(session, clock.now(), settings.max_goals_per_recovery)
    handled = 0
    for goal_id in goal_ids:
        with factory() as session:
            try:
                if CoordinationService(session, settings, registry, clock).recover(goal_id):
                    handled += 1
                session.commit()
            except Exception:  # noqa: BLE001
                session.rollback()
                logger.exception("goal recovery failed for %s", goal_id)
    return handled


def _purge_memory(
    *,
    factory: sessionmaker[Session],
    settings: Settings,
    clock: Clock,
) -> None:
    with factory() as session:
        store = MemoryStore(session, clock, settings)
        store.purge_due()
        store.expire_operational_proposals()
        session.commit()


def probe_database(factory: sessionmaker[Session]) -> bool:
    """Return whether the database answers. The exception text is not logged."""
    try:
        with factory() as session:
            session.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        logger.exception("worker database unavailable")
        return False


def eligible_goal_count(factory: sessionmaker[Session], clock: Clock, limit: int) -> int:
    with factory() as session:
        return len(recoverable_goal_ids(session, clock.now(), limit))


def serve(
    settings: Settings,
    stop: threading.Event,
    *,
    clock: Clock | None = None,
    status_out: list[WorkerStatus] | None = None,
) -> None:
    """Poll until asked to stop. The current pass finishes, then the process exits."""
    used = clock or SystemClock()
    status = WorkerStatus(settings.instance_id)
    if status_out is not None:
        status_out.append(status)
    server, bound = start_health_server(settings.worker_health_port, status)
    status.health_port = bound
    logger.info(
        "worker starting instance=%s interval=%s lease=%s shutdown_grace=%s health_port=%s",
        settings.instance_id,
        settings.worker_interval_seconds,
        settings.lease_seconds,
        settings.worker_shutdown_seconds,
        bound,
    )
    engine = create_db_engine(settings.database_url)
    factory = create_session_factory(engine)
    registry = build_default_registry()
    provider = build_provider(settings)
    try:
        while not stop.is_set():
            if probe_database(factory):
                try:
                    claimed = recover_available(
                        factory=factory,
                        settings=settings,
                        registry=registry,
                        provider=provider,
                        clock=used,
                    )
                    eligible = eligible_goal_count(factory, used, settings.max_goals_per_recovery)
                except SQLAlchemyError:
                    logger.exception("worker database unavailable")
                    status.mark_degraded(when=used.now())
                else:
                    status.mark_operational(when=used.now(), eligible=eligible, claimed=claimed)
                    snap = status.snapshot()
                    logger.info(
                        "worker poll instance=%s phase=operational eligible=%s claimed=%s "
                        "successes=%s failures=%s",
                        settings.instance_id,
                        snap["eligible_goals"],
                        snap["claimed_goals"],
                        snap["recovery_successes"],
                        snap["recovery_failures"],
                    )
            else:
                status.mark_degraded(when=used.now())
                logger.info(
                    "worker poll instance=%s phase=degraded eligible=0 claimed=0",
                    settings.instance_id,
                )
            if stop.wait(settings.worker_interval_seconds):
                break
    finally:
        status.mark_shutting_down()
        logger.info("worker stopped instance=%s", settings.instance_id)
        engine.dispose()
        server.shutdown()
        server.server_close()


def recover_once() -> int:
    """Recover tasks using process settings. Intended for one-shot invocation."""
    settings = load_settings()
    assert_runtime_is_allowed(settings)
    configure_logging(settings)
    engine = create_db_engine(settings.database_url)
    try:
        return recover_available(
            factory=create_session_factory(engine),
            settings=settings,
            registry=build_default_registry(),
            provider=build_provider(settings),
            clock=SystemClock(),
        )
    finally:
        engine.dispose()


def main() -> None:
    """Poll until SIGTERM or SIGINT. `--once` performs a single recovery pass."""
    parser = argparse.ArgumentParser(description="Resume interrupted Apex tasks.")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    assert_runtime_is_allowed(settings)
    configure_logging(settings)
    if args.once:
        count = recover_once()
        logger.info("recovery pass resumed %s task(s)", count)
        return
    stop = threading.Event()

    def _stop(_signum: int, _frame: object) -> None:
        logger.info("worker shutdown requested instance=%s", settings.instance_id)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    serve(settings, stop)


if __name__ == "__main__":
    main()
