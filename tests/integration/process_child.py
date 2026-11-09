"""Separate operating-system processes for PostgreSQL lease tests."""

from __future__ import annotations

import sys
import time

from sqlalchemy import create_engine, text

from apex.capabilities.builtins import build_default_registry
from apex.clock import SystemClock
from apex.config import Settings
from apex.coordination.leases import claim_goal
from apex.coordination.service import CoordinationService
from apex.persistence.models import Goal, Principal
from apex.persistence.session import create_session_factory
from apex.worker import recover_available


def _settings(url: str, owner: str) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=url,
        model_provider="mock",
        instance_id=owner,
        seed_dev_principals=False,
        dev_auth_enabled=False,
    )


def claim(url: str, goal_id: str, owner: str, expected: int) -> None:
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO process_barrier(name, owner) VALUES ('claim', :owner)"),
            {"owner": owner},
        )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM process_barrier WHERE name = 'claim'")
            ).scalar()
        if int(count or 0) >= expected:
            break
        time.sleep(0.02)
    else:
        raise SystemExit("claim barrier timed out")
    factory = create_session_factory(engine)
    with factory() as session:
        won = claim_goal(session, goal_id, owner, SystemClock().now(), 60, 3)
        session.commit()
        session.execute(
            text("INSERT INTO process_result(owner, claimed) VALUES (:owner, :claimed)"),
            {"owner": owner, "claimed": won},
        )
        session.commit()
    engine.dispose()


def advance_once(url: str, goal_id: str, owner: str) -> None:
    settings = _settings(url, owner)
    engine = create_engine(url)
    factory = create_session_factory(engine)
    with factory() as session:
        goal = session.get(Goal, goal_id)
        if goal is None:
            raise SystemExit("missing goal")
        actor = session.get(Principal, goal.principal_id)
        if actor is None:
            raise SystemExit("missing principal")
        CoordinationService(session, settings, build_default_registry(), SystemClock()).advance(
            goal_id, actor, max_waves=1
        )
    engine.dispose()


def recover(url: str, owner: str) -> None:
    settings = _settings(url, owner)
    engine = create_engine(url)
    factory = create_session_factory(engine)
    recover_available(
        factory=factory,
        settings=settings,
        registry=build_default_registry(),
        provider=None,
        clock=SystemClock(),
    )
    engine.dispose()


def main() -> None:
    mode, url, owner = sys.argv[1], sys.argv[2], sys.argv[3]
    if mode == "claim":
        claim(url, sys.argv[4], owner, int(sys.argv[5]))
        return
    if mode == "advance":
        advance_once(url, sys.argv[4], owner)
        return
    if mode == "recover":
        recover(url, owner)
        return
    raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
