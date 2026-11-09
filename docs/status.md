# Implementation status

This file describes the code in the repository. It is not a roadmap disguised as a feature list.

## Implemented

- FastAPI process with liveness (`/health`, `/v1/health`) and readiness (`/ready`).
- RS256 bearer authentication with issuer, audience, expiry, not-before, and algorithm restriction.
- Optional JWKS verification for a configured HTTPS URL, with cache, one bounded refresh for an unknown `kid`, and no PEM fallback when JWKS is configured.
- Internal principal mapping from issuer and subject. Callers cannot select a principal by header, query, or body.
- Development token signing that production startup refuses, including a private key, a partial auth configuration, development issuance, or a local-fixture network exception.
- `POST /v1/dev/tokens` only when issuance is explicitly enabled, the peer is loopback (or the test client), and `X-Apex-Dev-Bootstrap` matches the configured value. Forwarding headers are ignored.
- Default-deny grants with expiry and revocation. High-risk `allow` is evaluated as `require_approval`.
- One-hop delegation that cannot widen a grant or carry control-plane permissions.
- Execution-time comparison of a delegated payload's resource fields with the delegation scope. Direct calls do not gain that check. The scope is part of the operation hash.
- Approval binding to a canonical SHA-256 operation hash, including capability version, schema hash, and delegation resource scope.
- Separation of duties, approval expiry, replay rejection, and schema-change rejection.
- Lease compare-and-swap so a live lease is not taken by another worker. Horizontal scaling is unsupported: a worker that continues after lease expiry is not fenced. The supported deployment runs one worker.
- Uncertain outcome for a non-idempotent call whose result is lost after a possible write. The persisted execution, not the exception code, decides the goal. A `timed_out` exception does not mark that goal failed. The worker does not retry it, and replan does not repeat it.
- Authorized reconciliation of that outcome from fixture state (`possibly_sent`, `confirmed_success`, `confirmed_failure`, `inconclusive`, `not_sent`).
- In-process MCP fixture (`records_lookup`, `records_update`) discovered through the MCP SDK and executed only after explicit registration.
- Remote Streamable HTTP MCP registration with endpoint checks, secret references, and execution through the same engine. A schema change marks the tool unavailable until it is registered again.
- Independent verification of fixture reads and writes against the in-memory store.
- Deterministic intent for the original utterances plus the two fixture utterances.
- Mock provider plus optional OpenAI and Anthropic adapters behind one interface.
- Alembic revisions `0001_initial` through `0006_reliability`.
- One-shot migration command `python -m apex.persistence.migrate`. API processes do not migrate on startup.
- Production `/ready` requires the Alembic revision `0006_reliability`, an active authenticator, and a usable secret provider when an external provider is required.
- Task events, executions, verifications, and one trusted task-scoped context record after a passed verification.
- Governed memory with personal, project, task, and system scope. Versions, provenance, reliability, logical deletion, physical purge of expired deletions, deterministic retrieval, and a context budget. Model proposals stay inferred candidates. Memory does not grant authority. Purge tombstones do not store content. Derived context redaction, operational audit retention, and backup retention are separate and are documented in `docs/security/data-lifecycle.md`.
- Development token issuance rejects proxy headers even from a loopback peer, including `Via`, `X-Original-URL`, `CF-Connecting-IP`, and `X-Forwarded-Server`.
- Remote MCP connections use the pinned transport and an httpx client with `max_redirects` 0. That controls requests made with the client Apex passes to the SDK. It is not an operating-system guarantee about other sockets.
- Secret providers are `env`, `file`, and `vault` KV v2. Production that requires an external provider refuses `env` and does not fall back to it. The Vault client is tested with a mock transport.
- Coordinated goals for a fixed set of objectives. Logical agents are Coordinator, Planner, Researcher, Executor, and Verifier. A role is not a grant. Capability work uses `ExecutionEngine`. The worker claims a PostgreSQL lease on the goal and resumes `planned` and `running` goals without `POST /v1/goals/{id}/run`. Completed executions are not repeated. Uncertain work is not retried. A goal is `completed` only when the current plan revision has a passed verification. There is no A2A protocol. The supported deployment runs that worker beside the API. `APEX_DEFER_GOAL_EXECUTION` leaves a new goal for the worker instead of finishing it inside the request.
- OpenID Connect discovery for one configured HTTPS issuer, or a pinned JWKS URL. The token cannot choose the issuer or the JWKS URL.

## Partial

- Natural language coverage is a strict parser, not a general interpreter.
- Model calls are advisory only. There is no model-routing policy beyond the configured provider.
- Recovery is a worker polling PostgreSQL leases. Separate operating-system processes can compete for one goal, and a replacement resumes after the lease expires. It is not a durable queue and it is not exactly-once delivery of an external side effect.
- OpenAI and Anthropic adapters are implemented and tested with mocked HTTP. Live calls are unverified.
- OIDC discovery and JWKS fetches are implemented. Tests inject the documents. A live identity provider was not called.
- The Vault KV v2 client is implemented and tested with a mock HTTP transport. A production Vault server is not provisioned here.
- Reconciliation of fixture updates reads the in-process store. A remote server without that independent read stays inconclusive and returns a manual reconciliation path.
- The supported deployment does not make the service production-ready. Live Vault, live OIDC, and erasure of backups or WAL are not included.

## Deferred

- A paid or hosted MCP server, stdio MCP, and any A2A implementation. External agent interoperability stays deferred.
- Model-proposed goal graphs. Decomposition is a deterministic parser for the coordinated utterances only.
- Exactly-once delivery to external systems
- Embeddings and vector search
- Multimodal, voice, mobile, and desktop clients
- Distributed workers, Kubernetes, and external side effects beyond the in-memory fixture
- Billing, admin dashboards, and autonomous self-improvement
- Rewriting or expiring immutable database backups

## Verification recorded for the M5.2 remediation

Commands ran in `python:3.12-slim` against a local copy of this tree. PostgreSQL 16 for pytest was Compose service `docker-postgres-1` on `localhost:5432`. No Alembic revision was added. Schema remains `0006_reliability`.

- `ruff check .` — all checks passed
- `ruff format --check .` — 164 files already formatted
- `mypy` — no issues in 98 source files
- `pytest` with `APEX_POSTGRES_TEST_URL=postgresql+psycopg://apex:apex@host.docker.internal:5432/apex` — 150 passed, 1 skipped, 0 failed

The skipped test is `test_rendered_deploy_compose_requires_a_password_and_binds_loopback`, because that container image has no Docker CLI. The same test, run on the host with `python -m pytest --noconftest tests/security/test_deployment_compose.py`, passed. Missing `APEX_POSTGRES_PASSWORD` makes `docker compose config` fail. With a dummy password the rendered API port is `host_ip: 127.0.0.1` and `published: "8000"`. PostgreSQL and worker health are not published. `APEX_DEV_AUTH_ENABLED` is false and `APEX_ENVIRONMENT` is production.

`python -m tests.integration.deployment_probe` printed `deployment-ok` and exited 0. It used Compose project `apex-m52`, volume `apex_m52_disposable`, and image `apex-ai:m52`. The probe upgraded an empty database, downgraded to base, and upgraded again to `0006_reliability`. It completed a deferred goal without `/run`, restarted the API without replacing the worker, kept approval, cancellation, completion, and uncertainty in place, failed an exhausted goal, recovered after a worker kill without a second execution of the committed step, restarted the API and worker, and recovered from a database outage. The production image stayed fail-closed: development authentication did not start, and the acceptance worker stopped within its grace period. Container logs did not contain the key or the database password.

After cleanup, `docker-postgres-1` was still running, volume `docker_apex_pg_data` was still present, and its `alembic_version` was `0006_reliability`.

GitHub Actions was not run. These changes are uncommitted on `d832cf1`, and no push was authorized. The earlier successful run on `d832cf1` does not cover this remediation.

Live Vault and live OIDC were not called.

## Verification recorded for M5.2

Commands ran in `python:3.12-slim` against a local copy of this tree. PostgreSQL 16 for the pytest run was Compose service `docker-postgres-1` on `localhost:5432`.

- `ruff format --check .` — 160 files already formatted
- `ruff check .` — all checks passed
- `mypy` — no issues in 98 source files
- `pytest` with `APEX_POSTGRES_TEST_URL=postgresql+psycopg://apex:apex@host.docker.internal:5432/apex` — 138 passed, 0 skipped

`python -m tests.integration.deployment_probe` printed `deployment-ok`. It used Compose project `apex-m52`, a new volume named `apex_m52_disposable`, and image `apex-ai:m52`. The probe upgraded that empty database, downgraded to base, and upgraded again to `0006_reliability`. It then completed a deferred goal with a passed verification without calling `/run`, restarted the API without replacing the worker, kept an export goal at `awaiting_approval` with no execution, left cancelled, completed, and uncertain goals in place, failed an exhausted goal, killed the worker after the first committed execution, and showed a second worker could not advance that lease. After the lease expired, recovery finished the goal without a second execution of the committed step. Stopping the API and worker and starting them again completed an unfinished goal. Stopping PostgreSQL made worker `/health` return 503; starting it returned 200 with the same execution count. A purged memory stayed absent after that restart, and the task request text remained.

The same image, without development overrides, returned `/health` 200 with `auth_mode` `unconfigured` and `environment` `production`, `/ready` 503, `POST /v1/tasks` 401, and `POST /v1/dev/tokens` 404. Starting it with development authentication enabled failed. A worker from that image, with production settings and the disposable database, became healthy. `docker stop` of the acceptance worker exited 0 and logged the shutdown. Container logs did not contain the private key.

`docker volume ls` after `down -v` showed only `docker_apex_pg_data`. `docker-postgres-1` was running and its `alembic_version` was `0006_reliability`. No Alembic revision was added.

Live Vault and live OIDC were not called.

## Verification recorded 2026-11-09

Commands ran in `python:3.12-slim` against a local copy of this tree, because the development host's application control policy blocks local native extensions. PostgreSQL 16 was Compose service `docker-postgres-1` on `localhost:5432`.

M5.1:

- `ruff format --check .` — 150 files already formatted
- `ruff check .` — all checks passed
- `mypy` — no issues in 97 source files
- `pytest` with `APEX_POSTGRES_TEST_URL=postgresql+psycopg://apex:apex@host.docker.internal:5432/apex` — 127 passed, 0 skipped

That pytest run includes separate operating-system processes competing for one PostgreSQL goal lease, a killed worker resumed by a replacement process, memory purge checked through a new database connection, and the MCP transport tests. It also includes the SQLite migration downgrade and a disposable database on the existing server. The development volume `docker_apex_pg_data` was not removed.

A second PostgreSQL 16 container, Compose project `apex-m51-disposable`, used a new volume named `apex_m51_disposable` and host port 55432. `python -m tests.integration.fresh_volume_probe` upgraded that empty database to `0006_reliability`, downgraded to base, upgraded again, completed a goal through the worker, and returned production `/ready` 200. The probe printed `fresh-volume-ok`. `docker compose -p apex-m51-disposable down -v` removed that volume. `docker-postgres-1` stayed healthy and its `alembic_version` stayed `0006_reliability`.

`docker build -t apex-ai:dev -f infrastructure/docker/Dockerfile .` succeeded. The image build warned that `APEX_DEV_AUTH_ENABLED` is declared as an environment instruction. Its value in the image is `false`. A container from that image returned `/health` 200 with `auth_mode` `unconfigured`, `/ready` 503, `POST /v1/tasks` 401 `development_auth_rejected`, and `POST /v1/dev/tokens` 404.

Live Vault and live OIDC were not called. GitHub Actions had not been executed at the time of this local run.

The M5 run, before this recovery work, is kept here:

- `ruff format --check .` — 141 files already formatted
- `ruff check .` — all checks passed
- `mypy` — no issues in 95 source files
- `pytest` with the same Postgres URL — 98 passed in 30.31s

That earlier run left `alembic_version` at `0005_coordination` and did not downgrade PostgreSQL. The M5.1 pytest run upgrades that database to `0006_reliability`.

The earlier same-day run, before coordinated goals, is kept here:

- `ruff format --check .` — 134 files already formatted
- `ruff check .` — all checks passed
- `mypy` — no issues in 90 source files
- `pytest` with the same Postgres URL — 85 passed in 25.93s

GitHub Actions was not executed for either run. The workflow in `.github/workflows/ci.yml` already runs Ruff, Mypy, pytest, and `alembic upgrade head` on Python 3.12. Live OpenAI and Anthropic calls were not made.

## Earlier verification

The 2026-11-08 notes below describe an earlier run. They are not the current result.

## Verification recorded 2026-11-08

Commands were run in `python:3.12-slim` with a local copy of this tree, because the development host's application control policy blocks local native extensions. PostgreSQL 16 was the Compose service `docker-postgres-1`, healthy, on `localhost:5432`.

- `uv lock` and `uv sync` — resolved `mcp` 2.3.0 and `pyjwt` 2.15.1
- `ruff check .` — all checks passed after `ruff format`
- `mypy` — success, 73 source files
- `pytest` with `APEX_POSTGRES_TEST_URL=postgresql+psycopg://apex:apex@host.docker.internal:5432/apex` — 47 passed. That run applied Alembic through `0002_identity_mcp` on PostgreSQL and on a temporary SQLite database, including downgrade and re-upgrade.
- After the final fixture formatting change, `ruff format --check .`, `ruff check .`, and `mypy` passed, and `pytest` without `APEX_POSTGRES_TEST_URL` reported 46 passed and 1 skipped (`test_postgres_migration`).
- `docker build -t apex-ai:dev -f infrastructure/docker/Dockerfile .` succeeded. The image sets `APEX_ENVIRONMENT=production`, `APEX_DEV_AUTH_ENABLED=false`, and `APEX_SEED_DEV_PRINCIPALS=false`. It does not apply migrations.
- The running image returned `GET /health` 200 with `database: ok`, `environment: production`, and `auth_mode: unconfigured`. `GET /ready` returned 503 `not_ready`. `POST /v1/tasks` with only `X-Apex-Principal` returned 401 `development_auth_rejected`.

GitHub Actions has not been run. Live OpenAI and Anthropic calls were not made.

## Not production-ready

Development token minting, the in-process fixture, a pinned PEM instead of JWKS, and a local Postgres password in Compose are for development. Do not put this service on an untrusted network. An empty production auth configuration stays alive and reports not ready.
