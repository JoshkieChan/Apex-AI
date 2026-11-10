"""Prove the Compose deployment recovers goals without POST /v1/goals/{id}/run.

This uses project apex-m52 and volume apex_m52_disposable. It refuses a compose file
that names the development volume. Cleanup removes only that project.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from apex.config import Settings
from apex.identity.tokens import issue_dev_token

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "infrastructure" / "docker" / "compose.acceptance.yml"
COMPOSE = ["docker", "compose", "-p", "apex-m52", "-f", str(COMPOSE_FILE)]
HOST = os.environ.get("APEX_DEPLOY_HOST", "127.0.0.1")
API = f"http://{HOST}:18080"
PROD = f"http://{HOST}:18082"
PASSWORD = "acceptance-only-db-password"
PRIVATE_KEY = ""
SEQUENTIAL = (
    "Coordinate sequentially: count the words in 'alpha beta' then count the words in 'gamma'"
)
EXPORT = "Coordinate an export to finance-archive"
TOKEN = "DEPLOY-MEMORY-77 should disappear"
ISSUER = "https://apex.test/issuer"
AUDIENCE = "apex-api"
SECRETS: list[str] = []
DISPOSABLE_VOLUMES = ("apex_m52_disposable", "apex_m52_keys")


def main() -> None:
    os.environ["APEX_POSTGRES_PASSWORD"] = PASSWORD
    SECRETS.append(PASSWORD)
    text = "\n".join(
        line
        for line in COMPOSE_FILE.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )
    if "docker_apex_pg_data" in text or "apex_pg_data" in text or "5432:5432" in text:
        raise SystemExit("acceptance compose names the development database")
    user_volume = _exists(["docker", "volume", "inspect", "docker_apex_pg_data"])
    user_db = _exists(["docker", "inspect", "docker-postgres-1"])
    try:
        _reset()
        _keys()
        _run(COMPOSE + ["up", "-d", "--build", "--wait"], timeout=900)
        _guard_config()
        _round_trip_schema()
        normal = _normal_goal()
        _restart_api(normal)
        _approval_stays_blocked()
        _terminal_goals_stay_put()
        _worker_restart_does_not_duplicate()
        _restart_both()
        _database_outage()
        _production_image()
        _graceful_worker_stop()
        _scan_logs()
    finally:
        _cleanup()
    if user_volume and not _exists(["docker", "volume", "inspect", "docker_apex_pg_data"]):
        raise SystemExit("development volume docker_apex_pg_data is missing")
    if user_db and not _exists(["docker", "inspect", "docker-postgres-1"]):
        raise SystemExit("development database container is missing")
    print("deployment-ok", flush=True)


def _normal_goal() -> str:
    _run(COMPOSE + ["stop", "worker"])
    started = _submit(SEQUENTIAL)
    if started["status"] != "running":
        raise SystemExit(f"deferred goal returned {started['status']}")
    if any(row["execution_id"] is not None for row in started["subtasks"]):
        raise SystemExit("API executed a deferred goal")
    _run(COMPOSE + ["start", "worker"])
    finished = _wait_goal(started["id"], "completed")
    if finished["verifications"][0]["status"] != "passed":
        raise SystemExit("verification did not pass")
    print("scenario normal-execution", flush=True)
    return str(started["id"])


def _restart_api(goal_id: str) -> None:
    worker = _container_id("apex-m52-worker")
    _run(COMPOSE + ["restart", "api"], timeout=120)
    if _container_id("apex-m52-worker") != worker:
        raise SystemExit("API restart replaced the worker")
    _wait_ready()
    finished = _goal(goal_id)
    if finished["status"] != "completed":
        raise SystemExit("API restart changed the completed goal")
    _wait_worker()
    print("scenario api-restart", flush=True)


def _approval_stays_blocked() -> None:
    _run(COMPOSE + ["stop", "worker"])
    started = _submit(EXPORT)
    if started["status"] != "running":
        raise SystemExit("export goal was not deferred")
    _run(COMPOSE + ["start", "worker"])
    waiting = _wait_goal(started["id"], "awaiting_approval")
    if _execution_count(str(waiting["task_id"])) != 0:
        raise SystemExit("worker executed an approval-gated goal")
    print("scenario approval-preserved", flush=True)


def _terminal_goals_stay_put() -> None:
    _run(COMPOSE + ["stop", "worker"])
    cancelled = _submit(SEQUENTIAL)["id"]
    completed = _submit(SEQUENTIAL)["id"]
    uncertain = _submit(SEQUENTIAL)["id"]
    exhausted = _submit(SEQUENTIAL)["id"]
    _sql(
        "UPDATE goals SET status = 'cancelled', lease_owner = NULL, "
        "lease_expires_at = NULL WHERE id = %s",
        (cancelled,),
    )
    _sql("UPDATE goals SET status = 'completed' WHERE id = %s", (completed,))
    _sql("UPDATE goals SET status = 'uncertain' WHERE id = %s", (uncertain,))
    _sql(
        "UPDATE goal_subtasks SET status = 'uncertain', error_code = 'outcome_uncertain' "
        "WHERE goal_id = %s AND subtask_key = 's1'",
        (uncertain,),
    )
    _sql(
        "UPDATE goals SET status = 'running', recovery_attempts = 3, "
        "lease_expires_at = NOW() - INTERVAL '1 second', lease_owner = 'stale' "
        "WHERE id = %s",
        (exhausted,),
    )
    _run(COMPOSE + ["start", "worker"])
    time.sleep(3)
    if _status(cancelled) != "cancelled" or _status(completed) != "completed":
        raise SystemExit("worker resumed a terminal goal")
    if _status(uncertain) != "uncertain":
        raise SystemExit("worker resumed an uncertain goal")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and _status(exhausted) != "failed":
        time.sleep(0.5)
    if _status(exhausted) != "failed":
        raise SystemExit("exhausted goal was not failed")
    print("scenario terminal-and-exhausted", flush=True)


def _worker_restart_does_not_duplicate() -> None:
    override = ROOT / ".deploy-hold" / "hold.yml"
    override.parent.mkdir(exist_ok=True)
    override.write_text(
        "services:\n  worker:\n    environment:\n      APEX_TEST_HOLD_AFTER_SUBTASK: /hold/after\n",
        encoding="utf-8",
    )
    held = COMPOSE + ["-f", str(override)]
    rendered = _run(held + ["config"], timeout=60)
    if "APEX_DATABASE_URL" not in rendered or "APEX_TEST_HOLD_AFTER_SUBTASK" not in rendered:
        raise SystemExit("compose did not keep the worker database settings")
    _run(held + ["up", "-d", "--no-deps", "--force-recreate", "worker"], timeout=180)
    _wait_worker()
    started = _submit(SEQUENTIAL)
    _wait_held()
    before = _subtasks(started["id"])
    if before["s0"] is None or before.get("s1") is not None:
        raise SystemExit(f"unexpected partial progress {before}")
    _run(COMPOSE + ["--profile", "multi", "up", "-d", "--no-deps", "worker-b"], timeout=180)
    time.sleep(3)
    if _subtasks(started["id"]) != before:
        raise SystemExit("second worker advanced a leased goal")
    _run(["docker", "kill", "apex-m52-worker"])
    _sql(
        "UPDATE goals SET lease_expires_at = NOW() - INTERVAL '1 second' WHERE id = %s",
        (started["id"],),
    )
    finished = _wait_goal(started["id"], "completed", timeout=40)
    after = _subtasks(started["id"])
    if after["s0"] != before["s0"]:
        raise SystemExit("recovery repeated the committed execution")
    if _step_counts(str(finished["task_id"])):
        raise SystemExit("a plan step has more than one execution")
    _run(COMPOSE + ["stop", "worker-b"])
    _run(COMPOSE + ["up", "-d", "--no-deps", "--force-recreate", "worker"], timeout=180)
    _wait_worker()
    print("scenario worker-restart", flush=True)


def _restart_both() -> None:
    _run(COMPOSE + ["stop", "worker"])
    started = _submit(SEQUENTIAL)
    if started["status"] != "running":
        raise SystemExit("goal completed inside the API")
    _run(COMPOSE + ["stop", "api", "worker"])
    _run(COMPOSE + ["up", "-d", "api", "worker"], timeout=180)
    _wait_ready()
    _wait_worker()
    finished = _wait_goal(started["id"], "completed", timeout=60)
    if finished["verifications"][0]["status"] != "passed":
        raise SystemExit("recovered goal did not verify")
    print("scenario deployment-restart", flush=True)


def _database_outage() -> None:
    created = _send(
        "POST",
        "/v1/memory",
        {
            "scope_type": "personal",
            "category": "fact",
            "content": TOKEN,
            "source_type": "user",
            "source_id": "note",
            "confirmed": True,
        },
    )
    if created[0] != 200:
        raise SystemExit(safe(f"memory create failed {created}"))
    memory_id = str(created[1]["id"])
    task = _send(
        "POST",
        "/v1/tasks",
        {"request": f"Analyze the following text and report its word count: {TOKEN}"},
    )
    if task[0] not in {200, 201} or TOKEN not in str(task[1].get("request")):
        raise SystemExit("task did not retain its request")
    deleted = _send("DELETE", f"/v1/memory/{memory_id}", None)
    if deleted[0] != 200:
        raise SystemExit("memory delete failed")
    _sql("UPDATE memories SET deleted_at = NOW() - INTERVAL '48 hours' WHERE id = %s", (memory_id,))
    purged = _send("POST", "/v1/memory/purge", None, subject="dev-approver")
    if purged[0] != 200 or int(purged[1].get("purged", 0)) < 1:
        raise SystemExit(safe(f"purge failed {purged}"))
    before = _scalar("SELECT COUNT(*) FROM executions")
    _run(COMPOSE + ["stop", "postgres"])
    _wait_worker_status(503)
    _run(COMPOSE + ["start", "postgres"])
    _wait_worker()
    _wait_ready()
    if _scalar("SELECT COUNT(*) FROM executions") != before:
        raise SystemExit("database recovery created executions")
    found = _send("POST", "/v1/memory/search", {"text": TOKEN})
    if any(TOKEN in str(row.get("content")) for row in found[1].get("memories", [])):
        raise SystemExit("purged memory returned after the database restart")
    reread = _send("GET", f"/v1/tasks/{task[1]['id']}", None)
    if TOKEN not in str(reread[1].get("request")):
        raise SystemExit("task request was erased")
    print("scenario database-outage", flush=True)


def _production_image() -> None:
    _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            "apex-m52-prod",
            "-p",
            "127.0.0.1:18082:8000",
            "apex-ai:m52",
        ]
    )
    body = _wait_url(f"{PROD}/health", 200)
    if body.get("auth_mode") != "unconfigured" or body.get("environment") != "production":
        raise SystemExit(safe(f"production health was {body}"))
    status, _ready = _send_url(f"{PROD}/ready")
    if status != 503:
        raise SystemExit(f"production ready was {status}")
    denied, _payload = _send_url(
        f"{PROD}/v1/tasks",
        method="POST",
        payload={"request": "Analyze the following text and report its word count: hi"},
        headers={"X-Apex-Principal": "dev-user"},
    )
    if denied != 401:
        raise SystemExit(f"production task route returned {denied}")
    missing, _body = _send_url(
        f"{PROD}/v1/dev/tokens", method="POST", payload={"subject": "dev-user"}
    )
    if missing != 404:
        raise SystemExit(f"production token route returned {missing}")
    refused = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            "APEX_DEV_AUTH_ENABLED=true",
            "apex-ai:m52",
            "python",
            "-c",
            "from apex.config import assert_runtime_is_allowed, load_settings; "
            "assert_runtime_is_allowed(load_settings())",
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if refused.returncode == 0:
        raise SystemExit("production image accepted development authentication")
    if b"BEGIN" in refused.stdout or b"BEGIN" in refused.stderr:
        raise SystemExit("production refusal printed key material")
    database_url = f"postgresql+psycopg://apex:{PASSWORD}@postgres:5432/apex"
    env = os.environ.copy()
    env["APEX_DATABASE_URL"] = database_url
    _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            "apex-m52-prod-worker",
            "--network",
            "apex-m52_default",
            "-e",
            "APEX_ENVIRONMENT=production",
            "-e",
            "APEX_DEV_AUTH_ENABLED=false",
            "-e",
            "APEX_DATABASE_URL",
            "-e",
            "APEX_INSTANCE_ID=prod-worker",
            "-e",
            "APEX_WORKER_INTERVAL_SECONDS=1",
            "apex-ai:m52",
            "apex-worker",
        ],
        env=env,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        probe = subprocess.run(
            [
                "docker",
                "exec",
                "apex-m52-prod-worker",
                "python",
                "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health')",
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if probe.returncode == 0:
            break
        time.sleep(0.5)
    else:
        raise SystemExit("production worker did not become healthy")
    print("scenario production-image", flush=True)


def _graceful_worker_stop() -> None:
    _run(["docker", "stop", "-t", "25", "apex-m52-worker"], timeout=40)
    code = _run(["docker", "inspect", "-f", "{{.State.ExitCode}}", "apex-m52-worker"])
    if code.strip() != "0":
        raise SystemExit(f"worker exit code {code.strip()}")
    logs = _logs("apex-m52-worker")
    if "worker stopped" not in logs or "worker shutdown requested" not in logs:
        raise SystemExit("worker did not record a graceful shutdown")
    print("scenario graceful-shutdown", flush=True)


def _scan_logs() -> None:
    for name in (
        "apex-m52-api",
        "apex-m52-worker",
        "apex-m52-worker-b",
        "apex-m52-prod",
        "apex-m52-prod-worker",
    ):
        if not _exists(["docker", "inspect", name]):
            continue
        logs = _logs(name)
        if "BEGIN PRIVATE" in logs or "BEGIN RSA" in logs:
            raise SystemExit(f"{name} logs contain key material")
        for secret in SECRETS:
            if secret and secret in logs:
                raise SystemExit(f"{name} logs contain a secret")
    print("scenario log-scan", flush=True)


def _round_trip_schema() -> None:
    _run(COMPOSE + ["stop", "api", "worker"])
    _run(
        COMPOSE + ["run", "--rm", "--no-deps", "migrate", "alembic", "downgrade", "base"],
        timeout=180,
    )
    _run(
        COMPOSE + ["run", "--rm", "--no-deps", "migrate", "alembic", "upgrade", "head"],
        timeout=180,
    )
    version = _scalar("SELECT version_num FROM alembic_version")
    if version != "0007_interaction":
        raise SystemExit(f"schema revision {version}")
    _run(COMPOSE + ["up", "-d", "api", "worker"], timeout=180)
    _wait_ready()
    _wait_worker()
    print("scenario schema-round-trip", flush=True)


def _reset() -> None:
    subprocess.run(COMPOSE + ["down", "-v", "--remove-orphans"], check=False, timeout=180)
    for name in ("apex-m52-prod", "apex-m52-prod-worker"):
        subprocess.run(["docker", "rm", "-f", name], check=False, timeout=60)
    for volume in DISPOSABLE_VOLUMES:
        subprocess.run(["docker", "volume", "rm", volume], check=False, timeout=60)


def _keys() -> None:
    global PRIVATE_KEY
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    SECRETS.append(private_pem)
    PRIVATE_KEY = private_pem
    subprocess.run(["docker", "volume", "create", "apex_m52_keys"], check=True, timeout=30)
    _write_key("private.pem", private_pem)
    _write_key("public.pem", public_pem)
    settings = Settings(
        _env_file=None,
        environment="test",
        dev_auth_enabled=True,
        auth_issuer=ISSUER,
        auth_audience=AUDIENCE,
        auth_private_key_pem=private_pem,
    )
    SECRETS.append(issue_dev_token(settings, "dev-user"))
    SECRETS.append(issue_dev_token(settings, "dev-approver"))


def _write_key(name: str, content: str) -> None:
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "-v",
            "apex_m52_keys:/keys",
            "alpine",
            "sh",
            "-c",
            f"cat > /keys/{name} && chmod 644 /keys/{name}",
        ],
        input=content.encode(),
        check=True,
        timeout=120,
    )


def _guard_config() -> None:
    rendered = _run(COMPOSE + ["config"], timeout=60)
    if "docker_apex_pg_data" in rendered or "apex_pg_data" in rendered:
        raise SystemExit("rendered compose attached the development volume")


def _submit(objective: str) -> dict[str, object]:
    status, body = _send("POST", "/v1/goals", {"objective": objective})
    if status != 200:
        raise SystemExit(safe(f"goal submit failed {status} {body}"))
    return body


def _goal(goal_id: str) -> dict[str, object]:
    status, body = _send("GET", f"/v1/goals/{goal_id}", None)
    if status != 200:
        raise SystemExit(safe(f"goal read failed {status}"))
    return body


def _wait_goal(goal_id: str, expected: str, timeout: int = 40) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        status, body = _send("GET", f"/v1/goals/{goal_id}", None)
        last = str(body.get("status") or status)
        if status == 200 and last == expected:
            return body
        time.sleep(0.5)
    raise SystemExit(f"goal stayed {last}, expected {expected}")


def _status(goal_id: str) -> str:
    return str(_goal(goal_id)["status"])


def _wait_ready() -> None:
    _wait_url(f"{API}/ready", 200)


def _wait_worker() -> None:
    _wait_worker_status(200)


def _wait_worker_status(expected: int) -> None:
    script = (
        "import urllib.error, urllib.request\n"
        "try:\n"
        "    print(urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=2).status)\n"
        "except urllib.error.HTTPError as exc:\n"
        "    print(exc.code)\n"
        "except Exception:\n"
        "    print(0)\n"
    )
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        completed = subprocess.run(
            ["docker", "exec", "apex-m52-worker", "python", "-c", script],
            capture_output=True,
            timeout=15,
            check=False,
        )
        text = (completed.stdout or b"").decode(errors="replace").strip()
        if text == str(expected):
            return
        time.sleep(0.5)
    raise SystemExit(f"worker health did not become {expected}")


def _wait_url(url: str, expected: int) -> dict[str, object]:
    deadline = time.monotonic() + 60
    last = 0
    while time.monotonic() < deadline:
        status, body = _send_url(url)
        last = status
        if status == expected:
            return body
        time.sleep(0.5)
    raise SystemExit(safe(f"{url} stayed {last}\n{_logs('apex-m52-api')[-2000:]}"))


def _wait_held() -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        probe = subprocess.run(
            ["docker", "exec", "apex-m52-worker", "cat", "/hold/after"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if probe.returncode == 0 and b"held" in probe.stdout:
            return
        time.sleep(0.2)
    raise SystemExit(safe(_logs("apex-m52-worker")[-1500:]))


def _send(
    method: str, path: str, payload: dict[str, object] | None, subject: str = "dev-user"
) -> tuple[int, dict[str, object]]:
    return _send_url(
        API + path,
        method=method,
        payload=payload,
        headers={"Authorization": f"Bearer {_token(subject)}"},
    )


def _send_url(
    url: str,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    data = None if payload is None else json.dumps(payload).encode()
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, _json(response.read())
    except urllib.error.HTTPError as error:
        return error.code, _json(error.read())
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return 0, {}


def _token(subject: str) -> str:
    settings = Settings(
        _env_file=None,
        environment="test",
        dev_auth_enabled=True,
        auth_issuer=ISSUER,
        auth_audience=AUDIENCE,
        auth_private_key_pem=PRIVATE_KEY,
    )
    token = issue_dev_token(settings, subject)
    if token not in SECRETS:
        SECRETS.append(token)
    return token


def _json(raw: bytes) -> dict[str, object]:
    if not raw:
        return {}
    parsed = json.loads(raw.decode())
    if isinstance(parsed, dict):
        return parsed
    return {}


def _sql(statement: str, params: tuple[object, ...]) -> None:
    with psycopg.connect(_dsn(), connect_timeout=5) as connection:
        connection.execute(statement, params)
        connection.commit()


def _scalar(statement: str) -> object:
    with psycopg.connect(_dsn(), connect_timeout=5) as connection:
        row = connection.execute(statement).fetchone()
    if row is None:
        raise SystemExit(statement)
    return row[0]


def _subtasks(goal_id: str) -> dict[str, str | None]:
    with psycopg.connect(_dsn(), connect_timeout=5) as connection:
        rows = connection.execute(
            "SELECT subtask_key, execution_id FROM goal_subtasks WHERE goal_id = %s",
            (goal_id,),
        ).fetchall()
    return {str(key): value for key, value in rows}


def _execution_count(task_id: str) -> int:
    return int(_scalar_params("SELECT COUNT(*) FROM executions WHERE task_id = %s", (task_id,)))


def _step_counts(task_id: str) -> list[object]:
    with psycopg.connect(_dsn(), connect_timeout=5) as connection:
        return list(
            connection.execute(
                "SELECT plan_step_id FROM executions WHERE task_id = %s "
                "GROUP BY plan_step_id HAVING COUNT(*) > 1",
                (task_id,),
            ).fetchall()
        )


def _scalar_params(statement: str, params: tuple[object, ...]) -> object:
    with psycopg.connect(_dsn(), connect_timeout=5) as connection:
        row = connection.execute(statement, params).fetchone()
    if row is None:
        raise SystemExit(statement)
    return row[0]


def _dsn() -> str:
    return f"host={HOST} port=55433 dbname=apex user=apex password={PASSWORD}"


def _container_id(name: str) -> str:
    return _run(["docker", "inspect", "-f", "{{.Id}}", name]).strip()


def _logs(name: str) -> str:
    completed = subprocess.run(
        ["docker", "logs", name],
        capture_output=True,
        timeout=30,
        check=False,
    )
    return (completed.stdout + completed.stderr).decode(errors="replace")


def _exists(args: list[str]) -> bool:
    completed = subprocess.run(args, capture_output=True, timeout=30, check=False)
    return completed.returncode == 0


def _run(args: list[str], timeout: int = 180, env: dict[str, str] | None = None) -> str:
    print(" ".join(args), flush=True)
    completed = subprocess.run(args, capture_output=True, timeout=timeout, check=False, env=env)
    output = (completed.stdout + completed.stderr).decode(errors="replace")
    if completed.returncode != 0:
        raise SystemExit(safe(output[-4000:]))
    return output


def _cleanup() -> None:
    subprocess.run(["docker", "rm", "-f", "apex-m52-prod", "apex-m52-prod-worker"], check=False)
    subprocess.run(COMPOSE + ["--profile", "multi", "down", "-v", "--remove-orphans"], check=False)
    for volume in DISPOSABLE_VOLUMES:
        subprocess.run(["docker", "volume", "rm", volume], check=False)


def safe(text: str) -> str:
    redacted = text
    for secret in SECRETS:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


if __name__ == "__main__":
    main()
