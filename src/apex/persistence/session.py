"""Database engine and session factory."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


def create_db_engine(url: str) -> Engine:
    """Create an engine. In-memory SQLite uses one shared connection."""
    connect_args: dict[str, object] = {}
    kwargs: dict[str, object] = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        kwargs["pool_pre_ping"] = False
        if ":memory:" in url or url.rstrip("/") in {"sqlite", "sqlite+pysqlite:"}:
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, connect_args=connect_args, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(
            dbapi_connection: object, _connection_record: object
        ) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a session factory that keeps loaded attributes after commit."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True, class_=Session)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Yield a session and close it. Callers commit explicitly."""
    session = factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
