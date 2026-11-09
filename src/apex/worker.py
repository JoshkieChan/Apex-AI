"""Resume interrupted tasks and goals. PostgreSQL leases are the coordination authority."""

from __future__ import annotations

import argparse
import os
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass

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


@dataclass(frozen=True)
class RecoveryReport:
    """One recovery pass. A claim is not a confirmed success."""

    tasks_handled: int = 0
    tasks_succeeded: int = 0
    tasks_failed: int = 0
    tasks_uncertain: int = 0
    tasks_deferred: int = 0
    goals_claimed: int = 0
    goals_succeeded: int = 0
    goals_failed: int = 0
    goals_uncertain: int = 0
    goals_deferred: int = 0


def recover_available(
    *,
    factory: sessionmaker[Session],
    settings: Settings,
    registry: CapabilityRegistry,
    provider: ModelProvider | None,
    clock: Clock,
) -> RecoveryReport:
    """Resume expired task leases, decided approvals, and recoverable goals."""
    try:
        tasks = _recover_tasks(
            factory=factory,
            settings=settings,
            registry=registry,
            provider=provider,
            clock=clock,
        )
    except SQLAlchemyError:
        logger.exception("task recovery failed")
        raise
    try:
        goals = _recover_goals(factory=factory, settings=settings, registry=registry, clock=clock)
        _purge_memory(factory=factory, settings=settings, clock=clock)
    except SQLAlchemyError:
        logger.exception("goal recovery failed")
        raise
    return RecoveryReport(
        tasks_handled=tasks.tasks_handled,
        tasks_succeeded=tasks.tasks_succeeded,
        tasks_failed=tasks.tasks_failed,
        tasks_uncertain=tasks.tasks_uncertain,
        tasks_deferred=tasks.tasks_deferred,
        goals_claimed=goals.goals_claimed,
        goals_succeeded=goals.goals_succeeded,
        goals_failed=goals.goals_failed,
        goals_uncertain=goals.goals_uncertain,
        goals_deferred=goals.goals_deferred,
    )


def _recover_tasks(
    *,
    factory: sessionmaker[Session],
    settings: Settings,
    registry: CapabilityRegistry,
    provider: ModelProvider | None,
    clock: Clock,
) -> RecoveryReport:
    with factory() as session:
        expired = recoverable_task_ids(session, clock.now())
        ready = ready_after_approval_ids(session)
    expired_ids = set(expired)
    task_ids = list(dict.fromkeys([*expired, *ready]))
    handled = succeeded = failed = uncertain = deferred = 0
    for task_id in task_ids:
        with factory() as session:
            try:
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
                outcome = _task_outcome(session, task_id)
            except Exception:  # noqa: BLE001
                session.rollback()
                logger.exception("task recovery failed for %s", task_id)
                handled += 1
                failed += 1
                continue
            handled += 1
            if outcome == "succeeded":
                succeeded += 1
            elif outcome == "failed":
                failed += 1
            elif outcome == "uncertain":
                uncertain += 1
            else:
                deferred += 1
    return RecoveryReport(
        tasks_handled=handled,
        tasks_succeeded=succeeded,
        tasks_failed=failed,
        tasks_uncertain=uncertain,
        tasks_deferred=deferred,
    )


def _recover_goals(
    *,
    factory: sessionmaker[Session],
    settings: Settings,
    registry: CapabilityRegistry,
    clock: Clock,
) -> RecoveryReport:
    with factory() as session:
        exhausted = exhaust_goal_ids(session, clock.now(), settings.max_goal_recoveries)
    for goal_id in exhausted:
        with factory() as session:
            CoordinationService(session, settings, registry, clock).exhaust(goal_id)
            session.commit()
    with factory() as session:
        goal_ids = recoverable_goal_ids(session, clock.now(), settings.max_goals_per_recovery)
    claimed = succeeded = failed = uncertain = deferred = 0
    for goal_id in goal_ids:
        with factory() as session:
            service = CoordinationService(session, settings, registry, clock)
            try:
                result = service.recover(goal_id)
                session.commit()
            except Exception:  # noqa: BLE001
                session.rollback()
                logger.exception("goal recovery failed for %s", goal_id)
                if service.last_claim_acquired:
                    claimed += 1
                failed += 1
                continue
            if not result.claimed:
                continue
            claimed += 1
            if result.outcome == "succeeded":
                succeeded += 1
            elif result.outcome == "failed":
                failed += 1
            elif result.outcome == "uncertain":
                uncertain += 1
            elif result.outcome == "deferred":
                deferred += 1
    return RecoveryReport(
        goals_claimed=claimed,
        goals_succeeded=succeeded,
        goals_failed=failed,
        goals_uncertain=uncertain,
        goals_deferred=deferred,
    )


def _task_outcome(session: Session, task_id: str) -> str:
    task = get_task(session, task_id)
    if task is None or task.status == "failed":
        return "failed"
    if task.status == "completed":
        return "succeeded"
    if task.status == "uncertain":
        return "uncertain"
    return "deferred"


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
    poll_once: Callable[[], RecoveryReport] | None = None,
) -> None:
    """Poll until asked to stop. Shutdown waits only until the configured deadline."""
    used = clock or SystemClock()
    freshness = max(settings.worker_interval_seconds * 3, 1.0)
    status = WorkerStatus(settings.instance_id, freshness_seconds=freshness)
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

    def _once() -> None:
        if poll_once is not None:
            report = poll_once()
            status.mark_operational(
                when=used.now(),
                eligible=0,
                goals_claimed=report.goals_claimed,
                goals_succeeded=report.goals_succeeded,
                goals_failed=report.goals_failed,
                goals_uncertain=report.goals_uncertain,
                goals_deferred=report.goals_deferred,
                tasks_handled=report.tasks_handled,
                tasks_succeeded=report.tasks_succeeded,
                tasks_failed=report.tasks_failed,
                tasks_uncertain=report.tasks_uncertain,
            )
            return
        if not probe_database(factory):
            status.mark_degraded(when=used.now())
            logger.info(
                "worker poll instance=%s phase=degraded eligible=0 claimed=0",
                settings.instance_id,
            )
            return
        try:
            report = recover_available(
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
            return
        status.mark_operational(
            when=used.now(),
            eligible=eligible,
            tasks_handled=report.tasks_handled,
            tasks_succeeded=report.tasks_succeeded,
            tasks_failed=report.tasks_failed,
            tasks_uncertain=report.tasks_uncertain,
            goals_claimed=report.goals_claimed,
            goals_succeeded=report.goals_succeeded,
            goals_failed=report.goals_failed,
            goals_uncertain=report.goals_uncertain,
            goals_deferred=report.goals_deferred,
        )
        snap = status.snapshot()
        logger.info(
            "worker poll instance=%s phase=%s eligible=%s claimed_goals=%s "
            "tasks_handled=%s successes=%s failures=%s uncertain=%s deferred=%s",
            settings.instance_id,
            snap["status"],
            snap["eligible_goals"],
            snap["claimed_goals"],
            snap["tasks_handled"],
            snap["recovery_successes"],
            snap["recovery_failures"],
            snap["goals_uncertain"],
            snap["goals_deferred"],
        )

    try:
        while not stop.is_set():
            finished = threading.Event()

            def _run(gate: threading.Event = finished) -> None:
                try:
                    _once()
                finally:
                    gate.set()

            threading.Thread(target=_run, name="apex-worker-poll", daemon=True).start()
            while not finished.wait(0.05):
                if stop.is_set():
                    break
            if stop.is_set():
                status.mark_shutting_down()
                if not finished.wait(settings.worker_shutdown_seconds):
                    logger.info(
                        "worker shutdown deadline exceeded instance=%s grace=%s",
                        settings.instance_id,
                        settings.worker_shutdown_seconds,
                    )
                break
            if stop.wait(settings.worker_interval_seconds):
                break
    finally:
        status.mark_shutting_down()
        logger.info("worker stopped instance=%s", settings.instance_id)
        engine.dispose()
        server.shutdown()
        server.server_close()


def recover_once() -> RecoveryReport:
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
        report = recover_once()
        logger.info(
            "recovery pass tasks=%s goals=%s successes=%s failures=%s",
            report.tasks_handled,
            report.goals_claimed,
            report.tasks_succeeded + report.goals_succeeded,
            report.tasks_failed + report.goals_failed,
        )
        return
    stop = threading.Event()

    def _stop(_signum: int, _frame: object) -> None:
        logger.info("worker shutdown requested instance=%s", settings.instance_id)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    serve(settings, stop)
    # serve() returns when the shutdown wait ends. A blocked adapter thread is
    # not cancelled by ThreadPoolExecutor; ending the process is the deadline.
    # Docker's stop grace period is the SIGKILL backstop if this does not run.
    os._exit(0)


if __name__ == "__main__":
    main()
