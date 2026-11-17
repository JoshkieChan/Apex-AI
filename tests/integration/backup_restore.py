"""Prove a PostgreSQL custom-format dump can be restored.

Each run creates a uniquely named container, volume, and host port. Cleanup
removes only resources this run created and labeled. A pre-existing volume is
left in place.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import OperationalError

ROOT = Path(__file__).resolve().parents[2]
HOST = os.environ.get("APEX_DEPLOY_HOST", "127.0.0.1")
RUN = uuid.uuid4().hex[:12]
NAME = f"apex-m8-{RUN}"
VOLUME = f"apex_m8_{RUN}"
PORT = ""
PASSWORD = "backup-probe-only"
MARKER = "restore-marker"
URL = ""
PSYCOPG_URL = ""
CREATED: list[tuple[str, str]] = []


def main() -> None:
    global PORT, URL, PSYCOPG_URL
    PORT = _free_port()
    URL = f"postgresql+psycopg://apex:{PASSWORD}@{HOST}:{PORT}/apex"
    PSYCOPG_URL = f"postgresql://apex:{PASSWORD}@{HOST}:{PORT}/apex"
    if PORT == "5432" or "docker_apex_pg_data" in VOLUME:
        raise SystemExit("backup probe names the development database")
    user_volume = _exists(["docker", "volume", "inspect", "docker_apex_pg_data"])
    user_db = _exists(["docker", "inspect", "docker-postgres-1"])
    sentinel = f"apex-sentinel-{RUN}"
    try:
        _create_sentinel(sentinel)
        _reset()
        _run(["docker", "volume", "create", "--label", f"apex-run={RUN}", VOLUME])
        CREATED.append(("volume", VOLUME))
        _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                NAME,
                "--label",
                f"apex-run={RUN}",
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
        CREATED.append(("container", NAME))
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
        try:
            remove_if_owned("volume", sentinel, RUN)
        except SystemExit:
            pass
        else:
            raise SystemExit("cleanup accepted a volume this run did not create")
        if not _exists(["docker", "volume", "inspect", sentinel]):
            raise SystemExit("pre-existing sentinel volume was removed")
    finally:
        _cleanup()
        _remove_sentinel(sentinel)
    if user_volume and not _exists(["docker", "volume", "inspect", "docker_apex_pg_data"]):
        raise SystemExit("development volume docker_apex_pg_data is missing")
    if user_db and not _exists(["docker", "inspect", "docker-postgres-1"]):
        raise SystemExit("development database container is missing")
    print("backup-restore-ok", flush=True)


def remove_if_owned(kind: str, name: str, run_id: str) -> None:
    """Delete one resource only when its label belongs to this run."""
    if kind not in {"volume", "container"}:
        raise SystemExit(f"refusing to delete {kind}")
    if not _labeled(kind, name, run_id):
        raise SystemExit(f"refusing to delete {kind} {name}")
    args = ["docker", "rm", "-f", name] if kind == "container" else ["docker", "volume", "rm", name]
    completed = subprocess.run(args, capture_output=True, check=False)
    if completed.returncode != 0:
        output = (completed.stdout + completed.stderr).decode(errors="replace")
        raise SystemExit(output.replace(PASSWORD, "[redacted]")[-2000:])


def _labeled(kind: str, name: str, run_id: str) -> bool:
    key = (
        '{{index .Config.Labels "apex-run"}}'
        if kind == "container"
        else '{{index .Labels "apex-run"}}'
    )
    completed = subprocess.run(
        ["docker", "inspect", "--format", key, name],
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0 and completed.stdout.decode().strip() == run_id


def _create_sentinel(name: str) -> None:
    _run(["docker", "volume", "create", "--label", "apex-sentinel=1", name])


def _remove_sentinel(name: str) -> None:
    completed = subprocess.run(
        ["docker", "inspect", "--format", '{{index .Labels "apex-sentinel"}}', name],
        capture_output=True,
        check=False,
    )
    if completed.returncode == 0 and completed.stdout.decode().strip() == "1":
        subprocess.run(["docker", "volume", "rm", name], capture_output=True, check=False)


def _free_port() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return str(sock.getsockname()[1])


def _mark() -> None:
    with psycopg.connect(PSYCOPG_URL) as connection:
        connection.execute(
            "INSERT INTO runtime_controls (control_key, enabled, updated_at, updated_by) "
            "VALUES ('execution_suspended', false, NOW(), %s) "
            "ON CONFLICT (control_key) DO UPDATE SET updated_by = EXCLUDED.updated_by",
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


def _cleanup() -> None:
    for kind, name in reversed(CREATED):
        remove_if_owned(kind, name, RUN)
    CREATED.clear()


def _reset() -> None:
    """Drop only a labeled resource left by an interrupted run of this process."""
    return


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
