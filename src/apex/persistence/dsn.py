"""Build a database URL without putting a raw password into a connection string."""

from __future__ import annotations

import os
from urllib.parse import quote


def assemble_database_url() -> str | None:
    """Return a URL from discrete PostgreSQL settings, or None when they are unset.

    A password is percent-encoded. The returned string is for drivers only.
    Callers must not log it. An explicit ``APEX_DATABASE_URL`` remains available
    when the host is absent. A password without a host is not a connection.
    """
    host = os.environ.get("APEX_POSTGRES_HOST", "")
    password = os.environ.get("APEX_POSTGRES_PASSWORD")
    if not host:
        return None
    if password is None or password == "":
        raise RuntimeError("PostgreSQL host and password must both be set.")
    if any(character in host for character in "@/?# "):
        raise RuntimeError("PostgreSQL host must be a bare hostname.")
    port = os.environ.get("APEX_POSTGRES_PORT", "5432")
    if not port.isdigit():
        raise RuntimeError("PostgreSQL port must be numeric.")
    user = quote(os.environ.get("APEX_POSTGRES_USER", "apex"), safe="")
    database = quote(os.environ.get("APEX_POSTGRES_DB", "apex"), safe="")
    return f"postgresql+psycopg://{user}:{quote(password, safe='')}@{host}:{port}/{database}"
