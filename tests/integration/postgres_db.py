"""Create and drop a database on the configured PostgreSQL server."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, text


@contextmanager
def disposable_database(name: str) -> Iterator[str]:
    source = os.environ["APEX_POSTGRES_TEST_URL"]
    parts = urlsplit(source)
    admin_url = urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, ""))
    fresh_url = urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, ""))
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    try:
        yield fresh_url
    finally:
        closer = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with closer.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        closer.dispose()
