"""Prove a PostgreSQL custom-format dump can be restored.

This uses container apex-m8-backup, host port 55434, and volume apex_m8_backup.
It refuses to name the development database. Cleanup removes only that container
and volume.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import OperationalError

ROOT = Path(__file__).resolve().parents[2]
HOST = os.environ.get("APEX_DEPLOY_HOST", "127.0.0.1")
PORT = "55434"
NAME = "apex-m8-backup"
VOLUME = "apex_m8_backup"
PASSWORD = "backup-probe-only"
MARKER = "restore-marker"
URL = f"postgresql+psycopg://apex:{PASSWORD}@{HOST}:{PORT}/apex"
PSYCOPG_URL = f"postgresql://apex:{PASSWORD}@{HOST}:{PORT}/apex"


def main() -> None:
    if "5432:5432" in " ".join(_args()) or "docker_apex_pg_data" in VOLUME:
        raise SystemExit("backup probe names the development database")
    user_volume = _exists(["docker", "volume", "inspect", "docker_apex_pg_data"])
    user_db = _exists(["docker", "inspect", "docker-postgres-1"])
    try:
        _reset()
        _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                NAME,
                "-e",
                "POSTGRES_USER=apex",
                "-e",
                f"POSTGRES_PASSWORD={PASSWORD}",
                "-e",
                "POSTGRES_DB=apex",
                "-p",
                f"127.0.0.1:{PORT}:5432",
                "-v",
                f"{VOLUME}:/var/lib/postgresql/data",
                "postgres:16-alpine",
            ]
        )
        os.environ["APEX_DATABASE_URL"] = URL
        _wait_ready()
        _upgrade()
        _mark()
        dump = "/var/lib/postgresql/data/apex.dump"
        _exec(["pg_dump", "-U", "apex", "-d", "apex", "-Fc", "-f", dump])
        _exec(
            [
                "psql",
                "-U",
                "apex",
                "-d",
                "apex",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                (
                    "DROP SCHEMA public CASCADE; CREATE SCHEMA public; "
                    "GRANT ALL ON SCHEMA public TO apex;"
                ),
            ]
        )
        _exec(
            [
                "pg_restore",
                "-U",
                "apex",
                "-d",
                "apex",
                "--no-owner",
                "--exit-on-error",
                dump,
            ]
        )
        _check()
    finally:
        _reset()
    if user_volume and not _exists(["docker", "volume", "inspect", "docker_apex_pg_data"]):
        raise SystemExit("development volume docker_apex_pg_data is missing")
    if user_db and not _exists(["docker", "inspect", "docker-postgres-1"]):
        raise SystemExit("development database container is missing")
    print("backup-restore-ok", flush=True)


def _args() -> list[str]:
    return ["docker", "run", "-p", f"127.0.0.1:{PORT}:5432", VOLUME]


def _mark() -> None:
    with psycopg.connect(PSYCOPG_URL) as connection:
        connection.execute(
            "INSERT INTO runtime_controls (control_key, enabled, updated_at, updated_by) "
            "VALUES ('execution_suspended', false, NOW(), %s)",
            (MARKER,),
        )
        connection.commit()


def _check() -> None:
    with psycopg.connect(PSYCOPG_URL) as connection:
        marker = connection.execute(
            "SELECT updated_by FROM runtime_controls WHERE control_key = 'execution_suspended'"
        ).fetchone()
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    if marker is None or marker[0] != MARKER:
        raise SystemExit("restored control row does not match the dump")
    if version is None or version[0] != "0009_controls":
        raise SystemExit("restored schema revision does not match")


def _upgrade() -> None:
    last = "migration did not connect"
    for _ in range(5):
        try:
            command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        except OperationalError as exc:
            last = str(exc).replace(PASSWORD, "[redacted]")
            time.sleep(1)
            continue
        return
    raise SystemExit(last[-2000:])


def _wait_ready() -> None:
    last = "backup database did not become ready"
    for _ in range(40):
        try:
            with psycopg.connect(PSYCOPG_URL, connect_timeout=2) as connection:
                connection.execute("SELECT 1")
        except psycopg.Error as exc:
            last = str(exc).replace(PASSWORD, "[redacted]")
            time.sleep(1)
            continue
        return
    raise SystemExit(last[-500:])


def _exec(args: list[str]) -> None:
    _run(["docker", "exec", "-e", f"PGPASSWORD={PASSWORD}", NAME, *args])


def _reset() -> None:
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True, check=False)
    subprocess.run(["docker", "volume", "rm", VOLUME], capture_output=True, check=False)


def _exists(args: list[str]) -> bool:
    completed = subprocess.run(args, capture_output=True, check=False)
    return completed.returncode == 0


def _run(args: list[str]) -> None:
    completed = subprocess.run(args, capture_output=True, check=False)
    if completed.returncode != 0:
        output = (completed.stdout + completed.stderr).decode(errors="replace")
        raise SystemExit(output.replace(PASSWORD, "[redacted]")[-2000:])


if __name__ == "__main__":
    main()
