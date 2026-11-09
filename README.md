# Apex AI

Apex AI is a provider-independent intelligence runtime. This repository is the foundational implementation: a request becomes a typed intent, a validated plan, an authority decision, a controlled execution, an independent verification, and a persisted history.

It is not a chatbot wrapper, a workflow product, or a production control plane. Development authentication is explicitly local and is refused when `APEX_ENVIRONMENT=production`.

## What this milestone runs

1. `POST /v1/tasks` accepts a request for the principal named by a validated bearer token.
2. Apex classifies the request with a deterministic interpreter. Model output is advisory and cannot add capabilities or grant permission.
3. Apex builds a plan using only capabilities in the registry.
4. Permissions are evaluated again at the execution boundary. The default is deny.
5. `records.export` and the high-risk fixture update wait for a different principal who holds `authority.approve`.
6. `text.analyze`, the simulated export, and the local MCP fixture run in-process. They do not touch the network, shell, or filesystem.
7. Deterministic checks verify the result. A model note cannot overturn a failed check. Fixture updates are checked against the fixture store, not the tool's success claim.
8. The task, plan, approval, execution, verification, and a trusted task-scoped outcome are stored.

Supported utterances are intentionally narrow:

- `Analyze the following text and report its word count: <text>`
- `Count the words in the following text: <text>`
- `Export a record to the archive labeled <label> with content <json object>`
- `Count the words in '<text>' and export the analysis to the archive labeled <label>`
- `Look up fixture record <name>` after `records_lookup` is registered
- `Update fixture record <name> status to <status>` after `records_update` is registered

An unregistered fixture utterance completes as a clarification and does not execute. Anything else does the same.

## Local setup

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), and Docker for PostgreSQL.

```bash
uv sync
copy .env.example .env
docker compose -f infrastructure/docker/compose.yaml up -d
uv run alembic upgrade head
uv run apex-api
```

On PowerShell, `copy` works for the example env file. The API listens on `http://127.0.0.1:8000`. This Compose file starts PostgreSQL only. It does not start the API or the worker, and it uses host port 5432 and the volume `apex_pg_data`.

Development principals seeded when `APEX_SEED_DEV_PRINCIPALS=true`:

| External key | Grants |
| --- | --- |
| `dev-user` | `text.analyze` allow, `records.lookup` allow, `records.export` and `records.update` require approval |
| `dev-restricted` | none |
| `dev-approver` | `authority.approve` allow, `authority.grant` allow, `text.analyze` allow |
| `dev-registrar` | `capability.register` allow |
| `dev-denied` | `text.analyze` deny |

Local development signs RS256 tokens when `APEX_DEV_AUTH_ENABLED=true` and `APEX_AUTH_ISSUER`, `APEX_AUTH_AUDIENCE`, and both PEM keys are set. `POST /v1/dev/tokens` returns 404 in production and when development auth is off.

```bash
curl -s http://127.0.0.1:8000/v1/health
curl -s http://127.0.0.1:8000/ready
curl -s -X POST http://127.0.0.1:8000/v1/dev/tokens ^
  -H "Content-Type: application/json" ^
  -d "{\"subject\":\"dev-user\"}"
```

Use the returned `access_token` as `Authorization: Bearer ...`. `X-Apex-Principal` is not an identity.

The local API process finishes an eligible goal inside the request when `APEX_DEFER_GOAL_EXECUTION` is false. A one-shot resume of expired leases is:

```bash
uv run apex-worker --once
```

## Supported deployment

`infrastructure/docker/compose.deploy.yml` is the local production-mode scaffold. It starts PostgreSQL, applies migrations once, then starts the API and one continuous `apex-worker`. Set `APEX_POSTGRES_PASSWORD` first. Compose has no default password. The API is published on `127.0.0.1:8000` only. PostgreSQL and worker health are not published on the host. This file is not an internet deployment. Eligible `planned` and `running` goals resume without `POST /v1/goals/{id}/run`. Horizontal scaling is unsupported.

```bash
# Set APEX_POSTGRES_PASSWORD in the environment. Do not commit it.
docker compose -f infrastructure/docker/compose.deploy.yml up -d --build
```

That project uses the volume `apex_deploy_pg_data` and does not publish PostgreSQL. It does not attach the development volume. The image process runs as `nobody`, with development authentication off. `GET /health` can be 200 while `GET /ready` is 503 until the issuer, audience, and public key are configured. Worker health is inside the container on port 8001 and is 200 only after a fresh database poll. See `docs/architecture/deployment.md`.

## Tests and checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

The default test suite uses SQLite and the mock model provider. It does not call OpenAI or Anthropic. Set `APEX_POSTGRES_TEST_URL` to also apply migrations to PostgreSQL:

```bash
set APEX_POSTGRES_TEST_URL=postgresql+psycopg://apex:apex@localhost:5432/apex
uv run pytest tests/integration/test_migrations.py
uv run alembic upgrade head
```

## Layout

`src/apex` is one installable package. Modules keep the runtime boundaries: `api`, `runtime`, `intent`, `planning`, `agents`, `authority`, `capabilities`, `execution`, `verification`, `memory`, `providers`, `mcp`, and `persistence`. Separate deployable entry points live in `apps/apex-api` and `apps/apex-worker`. A third always-on runtime process is not pretended; recovery is a single-process poller.

## Documentation

- [AGENTS.md](AGENTS.md) — instructions for people and coding agents
- [Architecture](docs/architecture/overview.md)
- [Runtime lifecycle](docs/architecture/runtime-lifecycle.md)
- [Data model](docs/architecture/data-model.md)
- [Security model](docs/security/model.md)
- [HTTP API](docs/api/http.md)
- [Model adapters](docs/adapters/model-providers.md)
- [MCP boundary](docs/adapters/mcp.md)
- [Implementation status](docs/status.md)
- [Decisions](docs/decisions/)

## Safety of this milestone

The local demo does not open a shell, read arbitrary files, or send the export anywhere. The export capability returns a deterministic receipt. The MCP fixture keeps its records in memory. Development token signing is not a production identity provider. Do not expose this process as a production authority service. `GET /ready` is 503 until issuer, audience, and a public key are configured.
