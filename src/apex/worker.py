"""Resume interrupted tasks and goals. PostgreSQL leases are the coordination authority."""

from __future__ import annotations

import argparse
import time

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
        MemoryStore(session, clock, settings).purge_due()
        session.commit()


def recover_once() -> int:
    """Recover tasks using process settings. Intended for the worker command."""
    settings = load_settings()
    assert_runtime_is_allowed(settings)
    configure_logging(settings)
    engine = create_db_engine(settings.database_url)
    return recover_available(
        factory=create_session_factory(engine),
        settings=settings,
        registry=build_default_registry(),
        provider=build_provider(settings),
        clock=SystemClock(),
    )


def main() -> None:
    """Poll until interrupted. `--once` performs a single recovery pass."""
    parser = argparse.ArgumentParser(description="Resume interrupted Apex tasks.")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    try:
        while True:
            count = recover_once()
            logger.info("recovery pass resumed %s task(s)", count)
            if args.once:
                return
            time.sleep(settings.worker_interval_seconds)
    except KeyboardInterrupt:
        logger.info("worker stopped")
        return


if __name__ == "__main__":
    main()
