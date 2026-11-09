"""Alembic environment. The database URL comes from APEX_DATABASE_URL when set."""

from __future__ import annotations

import os
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, event, pool

from apex.persistence import models  # noqa: F401
from apex.persistence.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return os.environ.get("APEX_DATABASE_URL") or config.get_main_option("sqlalchemy.url") or ""


def run_migrations_offline() -> None:
    """Emit SQL without opening a connection."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against the configured database."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    if connectable.dialect.name == "sqlite":
        # pysqlite commits on DDL, which drops Alembic's version stamp while
        # leaving the tables behind. SQLAlchemy's recipe keeps one transaction.
        @event.listens_for(connectable, "connect")
        def _sqlite_connect(dbapi_connection: Any, _connection_record: object) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
            dbapi_connection.isolation_level = None

        @event.listens_for(connectable, "begin")
        def _sqlite_begin(connection: Any) -> None:
            connection.exec_driver_sql("BEGIN")

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
