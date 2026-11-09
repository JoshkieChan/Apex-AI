# Apex AI engineering instructions

These instructions apply to every coding agent and human change in this repository.

Read the architecture docs and the modules you are about to change before editing them. Do not invent a parallel structure when an extension point already exists.

## Product identity

Apex is a provider-independent intelligence and execution runtime: one coordinated system, multiple models, multiple capabilities, and explicit authority. A chat surface, an agent builder, an MCP monitor, or a workflow engine may later be an interface. None of those is the product identity.

Do not collapse the design into a single provider SDK or a single prompt.

## Architectural boundaries

Dependency direction:

- `apex.contracts` and `apex.config` do not import runtime engines.
- `apex.api` calls `apex.runtime`. It does not import `apex.execution` or `apex.verification`.
- `apex.intent` and `apex.planning` do not execute capabilities and do not grant permission.
- `apex.authority` does not import execution.
- `apex.execution` is the only place an adapter runs. It re-checks the registry, the input schema, and authority.
- `apex.verification` does not grant permission and cannot overturn a failed deterministic check.
- `apex.providers` normalize model calls. Provider payloads do not become domain objects without schema validation.
- `apex.mcp` may describe discovered tools. Discovery does not register or execute them.

Agents are roles in `apex.agents` for a single task, and logical agents in `apex.coordination` for a goal. A role is not a grant. The coordinator cannot bypass approval. Do not add a network service per role unless a later decision record says so. Do not add an A2A protocol here.

## Coding standards

- Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic.
- Type public functions. Do not silence type errors to get a green check.
- Keep functions cohesive. Do not put business rules in route handlers.
- Extend an existing module before adding a package.
- New dependencies need a reason in the change summary: what standard-library or existing dependency was insufficient, and how the dependency is maintained.
- Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and `uv run pytest` before claiming a change is done.

## Security

- Default deny. A missing grant is a denial.
- Re-check permission at execution time. A plan is not a grant.
- Model output cannot add a capability, skip approval, or change a verification status.
- Approvals bind the capability id, operation hash, payload, and bindings. A changed operation needs a new approval.
- The requesting principal cannot approve their own operation.
- Adapters in this milestone must not use `subprocess`, unrestricted filesystem access, or unrestricted network access.
- Do not log secrets, raw provider payloads, or full request bodies. Use `apex.redaction`.
- Do not return exception text, SQL, or credentials to clients.
- `APEX_DEV_AUTH_ENABLED` is development-only. Never enable it when `APEX_ENVIRONMENT=production`. `POST /v1/dev/tokens` also requires `APEX_DEV_TOKEN_ISSUANCE` and a local bootstrap header. Do not treat it as a public mint.
- Identity comes from a validated RS256 bearer token. Do not treat `X-Apex-Principal`, a body field, or a query parameter as authentication.
- Do not describe the development token issuer as a production identity provider.

## Persistence

- Schema changes go through Alembic. Do not call `create_all` on application startup. The current revision is `0006_reliability`.
- Keep relational columns for identity, status, and relationships. JSON is only for validated payloads.
- Use transactions around state transitions and execution records.
- Do not delete audit rows to make a test pass. Physical purge of an already deleted memory removes that memory's content rows, redacts matching derived context text, and leaves a tombstone without the content. It does not erase task, execution, approval, or goal audit rows, and it does not rewrite backups or WAL. Those are separate retention rules. See `docs/security/data-lifecycle.md`.
- Trusted memory and proposals stay distinct. Do not persist raw model transcripts as trusted memory.

## Task lifecycle

Legal states are `submitted`, `interpreting`, `planned`, `awaiting_approval`, `executing`, `verifying`, `completed`, `failed`, and `cancelled`. Use `apex.runtime.transitions.transition`. Do not assign `task.status` from a route or a model.

Terminal states do not transition. Duplicate submissions with the same principal and idempotency key return the existing task and must not execute again.

Goal tasks keep a null task lease. The worker resumes recoverable goals through a lease on `goals`, not through the task orchestrator. It resumes `planned` and `running` only. `awaiting_approval`, `uncertain`, `failed`, `partial`, `completed`, and `cancelled` stay put. A goal is `completed` only when the current plan revision has a passed verification.

## API contracts

- Versioned routes live under `/v1`.
- Changing a response field, status code, or error code is a contract change. Document it in `docs/api/http.md` and the change summary.
- Do not add a breaking field removal without stating the impact.

## Tests

- Tests must run without paid API keys.
- Do not skip, weaken, or delete a failing test to make the suite green. Fix the defect or record an explicit, reviewed limitation.
- Add tests for permission denial, approval binding, and verification when you change those paths.
- Prefer the mock provider and SQLite. PostgreSQL is covered when `APEX_POSTGRES_TEST_URL` is set and by `alembic upgrade head`.

## Secrets and environment

- Placeholders belong in `.env.example` only.
- Never commit `.env`, keys, tokens, or production data.
- Provider keys are read from the environment or from the configured secret provider. An empty key must fail the provider call, not fall back to a hidden credential.
- `env` is the development provider. Production that sets `APEX_REQUIRE_EXTERNAL_SECRETS=true` must use `file` or `vault` and must not fall back to `env`.

## Prohibited shortcuts

- Do not execute a capability id that is not in the registry.
- Do not let the planner be the only permission check.
- Do not treat a successful model response as proof that an operation succeeded.
- Do not add a distributed queue, Kubernetes, a vector database, or a new agent process as a side effect of a small fix.
- Governed memory lives in `apex.memory`. A memory row is not a grant, and model text cannot verify itself. Do not treat embeddings as proof.
- Do not claim A2A interoperability. The local fixture is in-process. Remote MCP is Streamable HTTP for an explicitly registered server only. Discovery does not grant execution. External agent interoperability is not part of coordinated goals.
- Do not treat an agent role, an autonomy label, or agreement between agents as authority or as verification. Do not retry an uncertain goal subtask.
- Do not mark the service production-ready because it starts locally.

## Documentation

Update `docs/status.md` when a behavior moves between implemented, partial, and deferred. Significant structural choices get a record in `docs/decisions/`. Docs describe the code that exists.
