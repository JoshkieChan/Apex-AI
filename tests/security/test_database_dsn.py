"""Database URLs encode reserved password characters and fail closed when incomplete."""

from __future__ import annotations

from urllib.parse import quote, unquote, urlsplit

import pytest

from apex.config import Settings
from apex.persistence.dsn import assemble_database_url


def test_password_reserved_characters_stay_in_the_password(monkeypatch: pytest.MonkeyPatch) -> None:
    password = "a@b:c/d%e?f#g&h+i"
    monkeypatch.delenv("APEX_DATABASE_URL", raising=False)
    monkeypatch.setenv("APEX_POSTGRES_HOST", "postgres")
    monkeypatch.setenv("APEX_POSTGRES_PASSWORD", password)
    monkeypatch.setenv("APEX_POSTGRES_USER", "apex")
    monkeypatch.setenv("APEX_POSTGRES_DB", "apex")
    url = assemble_database_url()
    assert url is not None
    parts = urlsplit(url)
    assert parts.hostname == "postgres"
    assert parts.username == "apex"
    assert parts.password is not None
    assert unquote(parts.password) == password
    assert quote(password, safe="") in url
    settings = Settings(_env_file=None)
    assert settings.database_url == url
    assert password not in settings.model_dump().get("database_url", "").replace(
        quote(password, safe=""), ""
    )


def test_explicit_database_url_is_not_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APEX_DATABASE_URL", raising=False)
    monkeypatch.setenv("APEX_POSTGRES_HOST", "postgres")
    monkeypatch.setenv("APEX_POSTGRES_PASSWORD", "a@b")
    chosen = "sqlite+pysqlite:///:memory:"
    settings = Settings(_env_file=None, database_url=chosen)
    assert settings.database_url == chosen


def test_password_without_host_is_not_a_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APEX_POSTGRES_HOST", raising=False)
    monkeypatch.setenv("APEX_POSTGRES_PASSWORD", "a@b:c/d%e?f#g&h+i")
    assert assemble_database_url() is None


def test_host_without_password_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APEX_POSTGRES_HOST", "postgres")
    monkeypatch.delenv("APEX_POSTGRES_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="both be set"):
        assemble_database_url()


def test_absent_discrete_settings_leave_the_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APEX_DATABASE_URL", raising=False)
    monkeypatch.delenv("APEX_POSTGRES_HOST", raising=False)
    monkeypatch.delenv("APEX_POSTGRES_PASSWORD", raising=False)
    assert assemble_database_url() is None
