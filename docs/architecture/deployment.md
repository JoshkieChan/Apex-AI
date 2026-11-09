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

`infrastructure/docker/compose.deploy.yml` is the supported stack. Host port 5433, volume `apex_deploy_pg_data`. It does not attach the development volume.

```bash
docker compose -f infrastructure/docker/compose.deploy.yml up -d --build
docker compose -f infrastructure/docker/compose.deploy.yml restart worker
docker compose -f infrastructure/docker/compose.deploy.yml logs worker
```

The image default command is the API, as `nobody`, with `APEX_ENVIRONMENT=production` and `APEX_DEV_AUTH_ENABLED=false`. The worker service replaces the command with `apex-worker`. Both use the image built from `infrastructure/docker/Dockerfile`. Neither process migrates on startup.

## What starts

1. PostgreSQL becomes ready.
2. `migrate` upgrades an empty or older database to `0006_reliability` and exits.
3. The API and the worker start. Neither waits for the other.
4. The API serves `/health` when it can query the database. `/ready` stays 503 until authentication is configured and the schema revision matches.
5. The worker binds `APEX_WORKER_HEALTH_PORT` (8001). `/live` means the process is up. `/health` is 200 only when the phase is `operational`, which requires a successful poll. `starting`, `degraded`, and `shutting_down` return 503.

## Recovery

`APEX_DEFER_GOAL_EXECUTION=true` in the supported stack makes `POST /v1/goals` persist the goal and return it without running subtasks. The worker claims `planned` and `running` goals whose lease is null or expired. It does not need `POST /v1/goals/{id}/run`.

An API restart does not stop the worker. The worker keeps its lease and its poll loop.

A worker restart after SIGTERM finishes the current pass and releases the lease in that pass. A SIGKILL leaves the lease until `APEX_LEASE_SECONDS`. The next worker reclaims it. A succeeded execution is not run again. An uncertain execution is not retried. A goal in `awaiting_approval`, `uncertain`, `failed`, `partial`, `completed`, or `cancelled` stays there. A goal that has used `APEX_MAX_GOAL_RECOVERIES` fails with `recovery_exhausted` and does not start work.

Two worker containers use the same lease update. One holds an unexpired lease. The other does not advance that goal. No second broker is required.

If PostgreSQL is down, the worker logs the exception type, sets phase `degraded`, and returns 503. It does not claim goals and does not report a successful recovery. When the database accepts connections again, the next poll continues from the rows that are still there.

## Configuration

The API and the worker read `APEX_` settings. Lease duration, recovery limits, and deferral are the same names in both processes. Set a distinct `APEX_INSTANCE_ID` on each so a lease release matches the owner that claimed it.

`APEX_WORKER_INTERVAL_SECONDS` is the pause between polls. `APEX_WORKER_SHUTDOWN_SECONDS` is the grace the process records at startup. The Compose `stop_grace_period` is slightly longer so Docker does not kill the process first.

`APEX_AUTH_PUBLIC_KEY_FILE` and `APEX_AUTH_PRIVATE_KEY_FILE` fill the PEM settings when those values are empty. A missing file raises an error that does not include the path or the key. Production still refuses a private key.

## Fresh volume

`tests/integration/deployment_probe.py` creates project `apex-m52`, volume `apex_m52_disposable`, and ports 55433, 18080, and 18081. It builds the image, upgrades an empty database, downgrades to base, upgrades again, and runs the API and worker. It does not call `/run`. Cleanup is `docker compose -p apex-m52 -f infrastructure/docker/compose.acceptance.yml down -v` plus removal of `apex_m52_disposable` and `apex_m52_keys` only.

```bash
uv run python -m tests.integration.deployment_probe
```

Do not point that file at port 5432 or `docker_apex_pg_data`.

## Logs

Worker lines include the instance id, phase, eligible-goal count, claimed count, and success and failure counts. The JSON formatter records `exception_type` and redacts URL passwords. It does not record the exception text. SQLAlchemy's own loggers are not propagated. The health body has the same counters and does not include a query, a token, or a goal objective.
