# Deployment

Apex runs as one image and two long-lived processes. They share PostgreSQL, the same settings names, and the same execution and authority code. The worker is not an API thread.

## Services

| Service | Role |
| --- | --- |
| `postgres` | PostgreSQL 16. The only coordination store. |
| `migrate` | Runs `alembic upgrade head` once, then exits. |
| `api` | Uvicorn. Accepts requests. Does not resume goals by itself when defer is on. |
| `worker` | `apex-worker`. Polls independently of API requests. |

`infrastructure/docker/compose.yaml` is the development database only. Host port 5432, volume `apex_pg_data`. It does not start the API or the worker.

`infrastructure/docker/compose.deploy.yml` is the local production-mode scaffold. Volume `apex_deploy_pg_data`. It does not publish PostgreSQL or the worker, and it binds the API to `127.0.0.1:8000`. It does not attach the development volume. It is not an internet-reachable deployment. Exposing the API beyond loopback requires TLS and a reverse proxy that this file does not provide. Authentication stays fail-closed until identity is configured outside Compose.

`APEX_POSTGRES_PASSWORD` is required. Compose stops if it is unset. There is no default password. The same value is interpolated into `APEX_DATABASE_URL` for the migrate, API, and worker services. The application secret provider is unchanged: it resolves `env:`, `file:`, and `vault:` references and does not supply the database password.

```bash
# PowerShell
$env:APEX_POSTGRES_PASSWORD = "set-this-outside-the-repo"
docker compose -f infrastructure/docker/compose.deploy.yml up -d --build
```

Horizontal scaling is unsupported. Run one worker. A second process will not take a lease that has not expired, and a worker that is still alive after expiry is not fenced. Do not add another worker service to this file.

The image default command is the API, as `nobody`, with `APEX_ENVIRONMENT=production` and `APEX_DEV_AUTH_ENABLED=false`. The worker service replaces the command with `apex-worker`. Both use the image built from `infrastructure/docker/Dockerfile`. Neither process migrates on startup.

## What starts

1. PostgreSQL becomes ready.
2. `migrate` upgrades an empty or older database to `0008_connectors` and exits.
3. The API and the worker start. Neither waits for the other.
4. The API serves `/health` when it can query the database. `/ready` stays 503 until authentication is configured and the schema revision matches.
5. The worker listens on `APEX_WORKER_HEALTH_PORT` inside the container (8001). That port is not published on the host. `/live` means the process is up. `/health` is 200 only when the phase is `operational`, the process is not shutting down, and the last successful poll is newer than three poll intervals (at least one second). A stuck poll becomes `degraded` and returns 503. `starting` and `shutting_down` return 503. The body separates tasks handled, goals claimed, confirmed successes, recovery failures, poll failures, uncertain recoveries, and deferred recoveries. A claim is not a success. A database poll failure increments `poll_failures` and does not increment `recovery_failures`. A completed poll does not mean a recovery succeeded.

## Recovery

`APEX_DEFER_GOAL_EXECUTION=true` in the supported stack makes `POST /v1/goals` persist the goal and return it without running subtasks. The worker claims `planned` and `running` goals whose lease is null or expired. It does not need `POST /v1/goals/{id}/run`.

An API restart does not stop the worker. The worker keeps its lease and its poll loop.

A worker restart after SIGTERM stops new polls and waits up to `APEX_WORKER_SHUTDOWN_SECONDS` for the current pass. If that pass finishes, it releases the lease. If it does not, the process ends and the lease remains until `APEX_LEASE_SECONDS`. That deadline does not cancel an adapter thread. `ThreadPoolExecutor.shutdown` cannot stop a call that is already running. The process exits and abandons the thread. Compose `stop_grace_period` is 25 seconds, five seconds longer than the default deadline, so Docker does not send SIGKILL before that wait ends. SIGKILL is the container backstop if the process has not exited. A succeeded execution is not run again. An execution whose `outcome_certainty` is `possibly_sent` stays uncertain and is not retried, including when the worker died before the subtask stored the execution id. A goal in `awaiting_approval`, `uncertain`, `failed`, `partial`, `completed`, or `cancelled` stays there. A goal that has used `APEX_MAX_GOAL_RECOVERIES` fails with `recovery_exhausted` and does not start work.

The acceptance probe can start a second worker while a lease is held. That only shows the second process does not advance an unexpired lease. It is not a supported topology and it is not a fencing test. Horizontal scaling is unsupported.

If PostgreSQL is down, the worker logs the exception type, sets phase `degraded`, and returns 503. It does not claim goals and does not report a successful recovery. When the database accepts connections again, the next poll continues from the rows that are still there.

## Configuration

The API and the worker read `APEX_` settings. Lease duration, recovery limits, and deferral are the same names in both processes. Set a distinct `APEX_INSTANCE_ID` on each so a lease release matches the owner that claimed it.

`APEX_WORKER_INTERVAL_SECONDS` is the pause between polls. `APEX_WORKER_SHUTDOWN_SECONDS` is the deadline after SIGTERM. The Compose `stop_grace_period` is slightly longer so Docker does not kill the process first. A pass that is still running at the deadline is abandoned. The worker does not claim that the abandoned work completed.

`APEX_AUTH_PUBLIC_KEY_FILE` and `APEX_AUTH_PRIVATE_KEY_FILE` fill the PEM settings when those values are empty. A missing file raises an error that does not include the path or the key. Production still refuses a private key.

Uploaded media stays on the API process. An empty `APEX_MEDIA_DIR` uses a private directory under the temporary filesystem, which `nobody` can write. The worker does not read those files. One API process is the supported topology for media. `APEX_CORS_ORIGINS` is empty unless a browser origin is configured. Production rejects `*`.

## Fresh volume

`tests/integration/deployment_probe.py` creates project `apex-m52`, volume `apex_m52_disposable`, API `127.0.0.1:18080`, and PostgreSQL `127.0.0.1:55433`. Worker health is checked inside the container and is not published. The probe sets `APEX_POSTGRES_PASSWORD` for that run. It builds the image, upgrades an empty database, downgrades to base, upgrades again, and runs the API and one worker. It does not call `/run`. Cleanup is `docker compose -p apex-m52 -f infrastructure/docker/compose.acceptance.yml down -v` plus removal of `apex_m52_disposable` and `apex_m52_keys` only.

```bash
uv run python -m tests.integration.deployment_probe
```

Do not point that file at port 5432 or `docker_apex_pg_data`.

## Logs

Worker lines include the instance id, phase, eligible-goal count, goals claimed, tasks handled, and success, failure, uncertain, and deferred counts. Confirmed successes are completed goals and completed tasks. The JSON formatter records `exception_type` and redacts URL passwords. It does not record the exception text. SQLAlchemy's own loggers are not propagated. The health body has the same counters and does not include a query, a token, a password, or a goal objective. `/live` stays 200 while `/health` is 503.
