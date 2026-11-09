# 0011. Coordination recovery, verification, and the remaining trust gaps

Goal recovery uses the database that already stores the goal. Verification is a persisted decision for one plan revision. Secret resolution, remote uncertainty, and memory purge stay inside the existing boundaries.

## Decision

Recoverable goals are `planned` and `running`. The worker claims `goals.lease_owner` with a conditional update. A second worker cannot take an unexpired lease. Reclaiming an expired lease increments `recovery_attempts`. Picking up a goal whose lease is already null does not. When the count reaches `APEX_MAX_GOAL_RECOVERIES`, the goal fails with `recovery_exhausted` and no new execution starts. The parent task lease stays null, so the task orchestrator still does not run the graph.

A stale `running` subtask is repaired from its execution row. A succeeded execution is marked completed and is not run again. An uncertain or `possibly_sent` execution stays uncertain and is not retried. An idempotent subtask with no execution can return to `pending` while attempts remain. Anything else fails as `stale_invocation`.

The verifier calls the capability checker and writes `goal_verifications` for the current revision. `completed` requires a `passed` row on that revision. `inconclusive` and `failed` do not complete the goal. `assess_claim` returns `inconclusive` without evidence and `partially_verified` for a shared-model assessment. Neither completes a goal.

`file` and `vault` are secret providers. `env` remains for development and tests. Production with `APEX_REQUIRE_EXTERNAL_SECRETS=true` rejects `env` and does not fall back to it. References are `file:NAME` and `vault:path`. Values are not stored in the database.

Remote MCP uses the httpx client Apex constructs, with redirects disabled and the pinned transport. A remote operation without an in-process independent read stays inconclusive and names a manual check. It is not retried.

OpenID Connect discovery reads only `{configured issuer}/.well-known/openid-configuration`. A pinned `APEX_AUTH_JWKS_URL` wins. Tokens cannot choose `jku`, `x5u`, or a JWKS URL.

Logical delete remains. After `APEX_MEMORY_PURGE_AFTER_HOURS`, the worker and `POST /v1/memory/purge` delete the memory, its revisions, and its events. `memory_purges` has no content. Backups are outside this process. `APEX_BACKUP_RETENTION_DAYS` records the operator expectation and does not erase a backup.

Development token issuance also rejects `Via`, `X-Original-URL`, `CF-Connecting-IP`, and `X-Forwarded-Server`. Production startup still refuses issuance.

## Consequences

Schema revision is `0006_reliability`. `/ready` in production requires that revision, an active authenticator, and a usable required secret provider. `/health` can be 200 while `/ready` is 503. The worker is still one process polling PostgreSQL. There is no second execution engine, no broker, and no exactly-once claim for external side effects. Vault and OIDC were tested with injected HTTP responses, not a live Vault or a live identity provider. Live Vault and live OIDC remain deferred. A goal advance commits the lease and each subtask before release, so a killed process leaves durable partial progress for another process to resume after the lease expires. The supported deployment runs that worker as its own service. Data retention is split in `0012-deployment-and-data-lifecycle.md`.
