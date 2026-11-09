# HTTP API

Base URL for local development: `http://127.0.0.1:8000`.

Successful task bodies use the `TaskView` schema from `apex.contracts.schemas`. Error bodies are:

```json
{"error": "code", "message": "safe message", "request_id": "..."}
```

Validation errors add `details` with `loc`, `msg`, and `type` only.

## Endpoints

| Method | Path | Auth | Result |
| --- | --- | --- | --- |
| GET | `/health`, `/v1/health` | no | liveness plus `schema_revision`, `secret_provider`, recoverable goals, leased goals, uncertain operations, and approvals waiting. Liveness stays 200 when those counts cannot be collected |
| GET | `/ready` | no | 200 when the database is up, bearer auth is active, production schema revision matches, and a required secret provider is usable; otherwise 503 |
| GET | `/v1/principal` | bearer | authenticated principal id and subject |
| POST | `/v1/dev/tokens` | local bootstrap | development token for a seeded subject; 404 unless issuance is enabled, the peer is local, and the bootstrap header matches |
| POST | `/v1/tasks` | bearer | 201 new task, or 200 when the idempotency key is replayed |
| GET | `/v1/tasks/{id}` | owner | full task, plan, approvals, executions, verifications, events |
| GET | `/v1/tasks/{id}/status` | owner | id, status, outcome, error |
| POST | `/v1/tasks/{id}/cancel` | owner | cancel before execution |
| GET | `/v1/approvals/{id}` | owner or approver | approval record |
| POST | `/v1/approvals/{id}/decision` | approver | approve or reject, then resume |
| POST | `/v1/mcp/servers` | registrar | add the `fixture` server or a remote HTTP server; the response has no credential value |
| POST | `/v1/mcp/servers/{server_key}/discover` | registrar | list tools; they stay unavailable |
| GET | `/v1/mcp/tools` | registrar | discovered and registered tools, without connection secrets |
| POST | `/v1/mcp/tools/{id}/register` | registrar | assign risk, permission, and verification kind |
| POST | `/v1/mcp/tools/{id}/disable` | registrar | disable a registered tool |
| POST | `/v1/mcp/tools/{id}/revoke` | registrar | revoke the tool and the server trust status |
| POST | `/v1/delegations` | bearer | one-hop delegation the caller already holds |
| POST | `/v1/grants/revoke` | grant administrator | set a grant to deny and stamp `revoked_at` |
| GET | `/v1/executions/{id}/reconciliation` | owner with `operations.reconcile` | certainty and the latest evidence |
| POST | `/v1/executions/{id}/reconcile` | owner with `operations.reconcile` | one reconciliation attempt; 409 when leased, exhausted, or not reconcilable |
| POST | `/v1/memory` | `memory.create` or `memory.manage` for system scope | create a governed memory; the owner is the caller |
| POST | `/v1/memory/search` | `memory.read` | authorized memories, ranked, excluding superseded and expired rows |
| POST | `/v1/memory/context` | `memory.read` | assembled context, governing instruction, included and excluded ids |
| POST | `/v1/memory/forget` | `memory.delete` | logical delete of one scope the caller can delete |
| GET | `/v1/memory/{id}` | `memory.read` | one visible memory; deleted and unauthorized ids are 404 |
| GET | `/v1/memory/{id}/history` | `memory.read` | revisions for a visible memory |
| PATCH | `/v1/memory/{id}` | `memory.update` | new version; 409 when `expected_version` is stale |
| POST | `/v1/memory/{id}/verify` | owner confirmation or `memory.verify` | sets `verified` only with an accepted evidence method |
| POST | `/v1/memory/{id}/dispute` | `memory.update` | marks the current version disputed |
| POST | `/v1/memory/{id}/supersede` | `memory.update` | closes the old version and creates a replacement |
| DELETE | `/v1/memory/{id}` | `memory.delete` | logical delete |
| POST | `/v1/memory/purge` | `memory.manage` | physically delete governed memories past the retention window. The response count is memories removed. It does not delete task, execution, approval, or goal rows |
| POST | `/v1/goals` | authenticated | submit a coordinated objective. Unknown text is 400 `not_a_goal`. When `APEX_DEFER_GOAL_EXECUTION` is true and `max_waves` is omitted, the response stays at zero waves for the worker |
| GET | `/v1/goals/{id}` | owner | goal, subtasks, invocation identity, verification status, and event names |
| POST | `/v1/goals/{id}/run` | owner | continue ready subtasks. The same claim is used by the worker |
| POST | `/v1/goals/{id}/replan` | owner | revise after an agent failure that had no execution. Pending approvals on that task are cancelled |
| POST | `/v1/goals/{id}/cancel` | owner | cancel pending work. Completed executions remain |
| GET | `/v1/goals/agents` | authenticated | role name, type, status, and autonomy |
| POST | `/v1/goals/agents/{key}` | `capability.register` | set `active`, `disabled`, or `retired` |

`X-Request-ID` is echoed or generated. `Idempotency-Key` may be a header or `idempotency_key` in the body. If both are set and differ, the response is 400 `idempotency_conflict`. Replays set `Idempotency-Replayed: true`.

## Create body

```json
{"request": "Analyze the following text and report its word count: hello", "project_key": "demo", "delegation_id": null}
```

`request` is required, 1 to 8000 characters. `project_key` is optional and is stored on the task. It selects project memory during context assembly. It does not by itself create project memory. A passed verification stores a task-scoped verified outcome. `delegation_id` is optional. When set, it must name an active delegation whose delegate is the caller.

Registration body:

```json
{"risk": "low", "permission_key": "records.lookup", "verification_kind": "records.lookup"}
```

`risk` is `low`, `medium`, or `high`. The permission key cannot be a control-plane permission.

## Decision body

```json
{"decision": "approved", "reason": "Reviewed the destination label."}
```

`decision` is `approved` or `rejected`. `reason` is required.

## Status codes

- 401 missing, invalid, expired, wrong issuer, wrong audience, unknown subject, or authentication not configured
- 400 `impersonation_rejected` when `principal_id` is in the query string
- 403 requester tried to self-approve, the caller cannot decide approvals, a delegation or registration is forbidden, or a goal assigns a capability the role cannot use (`role_forbidden`)
- 404 task, goal, approval, or agent is missing, it belongs to someone else, or the development token route is unavailable
- 409 illegal cancel, expired approval, already decided, replayed approval, schema change, the operation changed, reconciliation is leased or exhausted, or a goal replan meets uncertain work (`outcome_uncertain`)
- 422 invalid JSON body
- 503 `/ready` when auth, the database, the production schema revision, or a required secret provider is not usable
- 500 unexpected failure with a generic message

A task can be created with 201 and still finish as `failed`, `uncertain`, or `awaiting_approval`. Read `status` on the body. Permission denial, rejected approval, and failed verification are task results, not HTTP 403 from the execution engine. `auth_mode` is `unconfigured`, `development`, or `bearer`.
