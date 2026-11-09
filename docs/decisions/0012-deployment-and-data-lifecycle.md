# 0012. Worker deployment and the data lifecycle split

The API image was not a deployment of autonomous recovery, and memory purge was being described as if it erased every copy of a fact. Both are now explicit.

## Decision

The supported deployment is one image and two processes in `infrastructure/docker/compose.deploy.yml`. PostgreSQL, a one-shot Alembic upgrade, the API, and `apex-worker` start together. The worker reuses `recover_available`. It does not contain a second goal runner or a second MCP client. `APEX_DEFER_GOAL_EXECUTION` makes the API persist a goal at zero waves so the worker is what runs it. The development Compose file remains PostgreSQL only, on a different port and volume.

Worker health is a small HTTP server in the worker process. `/health` is 200 only after a poll that could query the database and only while that poll is still fresh. SIGTERM stops new polls and waits up to `APEX_WORKER_SHUTDOWN_SECONDS` for the current pass. If the pass does not finish, the process exits and the lease stays until it expires. SIGKILL does the same. The supported deployment runs one worker. A second process does not take an unexpired lease, and a worker that keeps running after expiry is not fenced. Horizontal scaling is unsupported.

Memory deletion stays the purge of `memories`, `memory_revisions`, and `memory_events`, plus a content-free tombstone. `context_records` are redacted for a casefolded, whitespace-collapsed copy of a deleted string when that normalized form is at least 12 characters. Casefold expansions such as `ß` and `ﬃ` are mapped back to the original span. Task, execution, approval, goal, and trusted context rows are operational audit and are not deleted to satisfy a memory purge. `APEX_OPERATIONAL_RETENTION_DAYS` defaults to 0. A positive value expires old untrusted context proposals that are not associated with unresolved work. A proposal whose scope id is an unresolved task, an unresolved goal, or the task of an unresolved goal stays. Backups and WAL stay operator-controlled.

No schema revision was added. The existing columns are enough.

## Consequences

Restarting the API does not resume goals. Restarting, or leaving running, the worker does. An operator who runs only `compose.yaml` still has to start `apex-worker` themselves. Production authentication is unchanged: the image refuses development auth, and `/ready` stays 503 until identity is configured. Live Vault and live OIDC remain deferred. The lease does not need Redis, and it also does not make two live workers safe. `0013-execution-certainty-and-deployment-limits.md` records that limit. Purge must not be documented as erasure of operational records or of backups. The Compose file is a local scaffold: the database password is required, PostgreSQL and worker health are not published, and the API binds to loopback.
