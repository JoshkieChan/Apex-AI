# Security model

This process can verify RS256 bearer tokens. It is not a production identity provider. When `APEX_AUTH_JWKS_URL` is set, verification uses that URL and does not fall back to a PEM. The token cannot choose the URL.

## Identity

Protected routes require `Authorization: Bearer`. `TokenAuthenticator` accepts only RS256 and checks the signature, issuer, audience, `exp`, `iat`, and `nbf`. The subject is mapped through `identity_subjects` to one internal principal. A caller cannot choose another principal with `X-Apex-Principal`, `principal_id` in the query string, or an unvalidated claim.

Development signing (`issue_dev_token`) runs only when the environment is not production and a private key is configured. `POST /v1/dev/tokens` also requires `APEX_DEV_TOKEN_ISSUANCE=true`, a configured bootstrap value in `X-Apex-Dev-Bootstrap`, and an immediate loopback peer. The test client is accepted only when the environment is `test`. `X-Forwarded-For` is not proof of locality. Production startup refuses a private key, development auth, development seeding, development issuance, a local-fixture network exception, an HTTP JWKS URL, and a partial issuer/audience/verifier configuration. An empty production auth configuration keeps the process up for liveness. `GET /health` stays 200 with `auth_mode: unconfigured`. `GET /ready` is 503 until authentication is configured and, in production, the Alembic revision is `0006_reliability`. Readiness is also 503 when an external secret provider is required and that provider is not usable. A loopback peer that also sends `X-Forwarded-For`, `Forwarded`, `X-Forwarded-Host`, `X-Forwarded-Proto`, `X-Real-IP`, `Via`, `X-Original-URL`, `CF-Connecting-IP`, or `X-Forwarded-Server` is rejected. The supported bootstrap is a process bound to loopback, with no proxy header, issuance enabled, and the bootstrap header set. Protected routes then return `authentication_not_configured` until that configuration exists.

Authentication failures use fixed messages. They do not return the key, the token, or a stack trace.

## Authority

`AuthorityEngine.evaluate` returns deny when no usable grant exists, when the effect is `deny`, when the grant is expired or revoked, and when the effect is unrecognized. `allow` and `require_approval` are the only other effects. A high-risk capability raises `allow` to `require_approval` at evaluation time. The execution engine calls this again after planning. A step does not store an allow bit the engine trusts.

`records.export` and `records.update` require approval for `dev-user`. `records.lookup` is an allow grant and is low risk. `dev-restricted` has no grants. `dev-denied` has an explicit deny for `text.analyze`. `dev-approver` can approve when `authority.approve` is allow and its `resource_scope` is `*` or matches the permission key or risk. `dev-registrar` can register capabilities. Holding a token does not grant those permissions.

A delegation is one hop. It cannot target the delegator, cannot include `authority.approve`, `authority.grant`, `capability.register`, `operations.reconcile`, `memory.manage`, or `memory.verify`, cannot outlive the delegator's grant, and cannot widen `resource_scope`. At execution, the delegate receives only the delegator's current direct grant for that permission. A chain is not consulted. The execution engine also compares resource fields declared by the capability with the delegation scope. `records.lookup`, `records.update`, and `records.export` declare those fields. A capability with no resource field fails closed when the scope is not `*`. A missing or mixed reference fails closed. Direct calls, where `delegation_id` is absent, are not filtered by grant scope. The current scope is inside the operation hash, so a later scope change invalidates the approval.

## Coordinated goals

A logical agent has a role and a lifecycle. Neither is a grant. The hard permission ceiling is in `apex.coordination.graph.HARD_PERMISSIONS`. A researcher cannot receive a write capability, a verifier cannot receive a capability, and widening `logical_agents.allowed_permissions` does not cross that ceiling. The requesting principal remains the principal on every invocation and on every execution row. The coordinator cannot approve an operation or skip `ExecutionEngine`.

An export assigned to the executor stays `awaiting_approval` until a separate approver accepts the existing approval contract. This milestone does not auto-approve. A principal without `text.analyze` fails the goal with `permission_denied` and no execution row.

Agent context is assembled through `MemoryStore.assemble`. Deleted, expired, and out-of-project memories are not placed in the invocation. The invocation stores memory ids, not memory content. The word-count verifier calls the registered deterministic checker and stores a `goal_verifications` row for the current revision. A missing output is `inconclusive`. A wrong output is `failed`. A goal becomes `completed` only when that revision has a passed verification. A passed row from an older revision does not count. Two agents agreeing is not a verification method. A model claim without evidence is `inconclusive`. A shared-model assessment with evidence is `partially_verified` and does not complete the goal.

A subtask marked uncertain is not retried and blocks replanning. Replanning a failed agent step that never executed cancels that step, cancels pending approvals for the goal task, keeps the completed execution, and records a new plan revision. Cancellation stops pending work and does not claim that a completed side effect was undone. The parent task lease stays null. The worker claims `goals.lease_owner` and resumes `planned` and `running` goals. It does not resume approval, uncertain, failed, partial, completed, or cancelled goals. Reclaiming an expired lease counts toward `APEX_MAX_GOAL_RECOVERIES`. A normal pickup of a free lease does not.

Agents cannot call `POST /v1/dev/tokens`, choose an issuer, or choose a JWKS URL. There is no agent-to-agent authority delegation and no A2A endpoint.

## Approvals

An approval stores the capability, operation hash, canonical payload, and bindings. The hash is SHA-256 of canonical JSON covering the capability id and version, schema hash, principal, delegation id, resource scope, payload, bindings, and upstream step hashes. Equivalent operations hash the same. A material change hashes differently.

The requester cannot decide their own approval. The approver must be authenticated and authorized for that permission or risk. Pending approvals expire after `APEX_APPROVAL_TTL_HOURS`. Changing the step payload or hash makes `decide` return `approval_mismatch`. A schema or version change after planning makes execution return `schema_changed`. A consumed approval cannot authorize another attempt. A prior uncertain attempt blocks a retry. Rejected, expired, and replayed approvals do not run the adapter again. `not_sent` does not count as an external attempt. `possibly_sent` is not treated as success or failure until reconciliation.

## Execution limits

Built-in adapters are pure functions. Registered MCP tools are called through the same execution engine. The engine rejects unknown or disabled capability ids before an adapter lookup. Inputs must match the capability schema and a byte limit. Adapters run with a timeout. An idempotent timeout is `timed_out`. A non-idempotent timeout, or a fixture update that is stored and then loses its response, is `uncertain`. Uncertain is not retried by the worker. Reconciliation is the only exit. The in-process fixture read can confirm a `records_update`. A remote server, including one that names a reconciliation capability, stays inconclusive unless that read is the in-process fixture. A tool response or a model note is not evidence. A lease allows one reconciler at a time. Apex does not claim exactly-once external execution.

There is no shell tool and no filesystem tool. The fixture dataset is in memory.

User JSON is stored as data. Bindings that pull a previous step's output live in `input_bindings`, so a user object cannot smuggle a reference by using a `$ref` key.

Task claims use a compare-and-swap on the lease. A second worker does not take a live lease. Execution attempts are unique on `(plan_step_id, attempt)`.

## MCP trust

Discovery does not register or enable a tool. The in-process fixture and an explicitly registered HTTPS server are the supported transports. Risk, permission, and verification kind are assigned by an authorized registrar, not by tool metadata. Schema changes are stored as a new capability version and block execution until the tool is registered again. The database stores `env:NAME`, `file:NAME`, or `vault:path` credential references. The value is read at call time and is not returned by the API. `file` reads a mounted file under `APEX_SECRET_DIR`. `vault` reads HashiCorp KV v2 from `APEX_VAULT_ADDR` with the token in the environment variable named by `APEX_VAULT_TOKEN_ENV`. Production with `APEX_REQUIRE_EXTERNAL_SECRETS=true` refuses the `env` provider and does not fall back to it. A file or Vault provider that cannot read the reference fails the call. Cache expiry is `APEX_SECRET_CACHE_SECONDS`. The Vault client was tested with a mock HTTP transport, not a live Vault server.

## Errors and logs

Unexpected errors return `internal_error` and a fixed message. Validation errors omit the submitted value. The JSON log formatter redacts common key shapes, bearer tokens, and URL passwords. Domain errors use fixed messages and do not echo the request body.

## Memory

Memory permissions are `memory.read`, `memory.create`, `memory.update`, `memory.delete`, `memory.manage`, and `memory.verify`. A missing grant is a denial. `memory.manage` and `memory.verify` cannot be delegated. Personal memory is readable by its owner. Project memory is readable when the grant or delegation scope allows that project. Task memory is readable by the task owner. System memory can be written only with `memory.manage`. A caller cannot name another principal as the owner.

A model proposal is stored as `inferred` and `candidate`. Repeating it does not verify it. User confirmation verifies only a user-sourced memory owned by that user. `authoritative_read` requires a passed execution of `mcp.fixture.records_lookup`. `independent_evidence` requires a passed verification row. Editing a verified memory returns it to `unverified`. Conflicting facts stay disputed. A newer confirmed user preference supersedes the older one. Superseded and expired memories are not current context.

Retrieved memory is attached as `MEMORY_DATA` after a fixed governing instruction. That instruction is not taken from memory content. Memory does not grant authority. Deletion sets `lifecycle` to `deleted` and keeps the row until purge. `POST /v1/memory/purge` requires `memory.manage` and physically deletes memories whose `deleted_at` is older than `APEX_MEMORY_PURGE_AFTER_HOURS`, including their revisions and events. The `memory_purges` tombstone stores the id, scope, revision count, and time. It does not store content. The worker runs the same purge for rows that are already deleted. `context_records` are task outcome rows, not the memory table. Purge redacts the deleted memory text out of their JSON when that text is at least 12 characters, and leaves the row so the task audit remains. Search, context assembly, and a later database session cannot return the deleted text. Apex does not rewrite database backups, base backups, or WAL archives. An operator who needs erasure from backups must expire those backups under `APEX_BACKUP_RETENTION_DAYS` and any external snapshot policy. That setting is the operator's retention expectation, not an erasure Apex performs.

## What is not safe to claim

- Development token issuance is safe behind a reverse proxy. Proxy headers are rejected, and a proxy that strips them can still make a public client look local. Keep issuance off except on a loopback development process.
- OpenID Connect discovery trusts only the configured issuer. Apex fetches `{issuer}/.well-known/openid-configuration` and requires the metadata issuer to match. A token cannot supply `jku`, `x5u`, `jwk`, or a different JWKS URL. When `APEX_AUTH_JWKS_URL` is set, that pinned URL is used and discovery is not. Discovery failure leaves authentication unconfigured. It does not crash liveness. Tests inject the discovery document. A live identity provider was not called.
- The export receipt is not an external transfer. The fixture is not an external system of record.
- Goal leases are conditional updates in PostgreSQL. Two workers cannot hold the same unexpired goal lease. After expiry, one worker may reclaim it. That reclaim is bounded. This is not a distributed queue, and it is not exactly-once delivery of an external side effect.
- Remote MCP uses MCP Python SDK 2.3.0 Streamable HTTP. Apex always passes its own `httpx2.AsyncClient`. The SDK creates a client only when `http_client` is omitted, and Apex does not omit it. The SDK follows a same-origin redirect only while `client.max_redirects` allows it; Apex sets that to 0, so a redirect response is returned and not followed, including an HTTP-to-HTTPS upgrade on the same host. `trust_env` is false, so `HTTP_PROXY` and `HTTPS_PROXY` are not inherited. Each request goes through `_PinnedTransport`, which rejects a host change, re-resolves DNS, and rejects an address change. POST, the SSE GET, and session DELETE share that client. Apex does not use the SDK's SSE client or OAuth auth flow. Production remote endpoints must be HTTPS; HTTP remains only for the development loopback fixture. The client also caps itself at four connections. This is the application boundary. It is not an operating-system guarantee that some other library in the process cannot open a socket. Model-provider HTTP clients are a separate path and are not an MCP bypass.
- A remote operation without an independent read stays `inconclusive`. The evidence includes `manual_reconciliation` and tells the operator not to retry the write. An MCP success response is not proof of external state.
- `file` and `vault` secret providers are implemented. The Vault tests use a mock HTTP client. A production Vault deployment is not provisioned by this repository.
- Physical purge removes eligible PostgreSQL memory rows and redacts that text from `context_records`. It does not expire an immutable backup or WAL segment.
- Model providers are optional HTTP clients. Their live APIs are not called by the test suite.
