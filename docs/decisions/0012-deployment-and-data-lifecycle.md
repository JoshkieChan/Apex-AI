# 0012. Worker deployment and the data lifecycle split

The API image was not a deployment of autonomous recovery, and memory purge was being described as if it erased every copy of a fact. Both are now explicit.

## Decision

The supported deployment is one image and two processes in `infrastructure/docker/compose.deploy.yml`. PostgreSQL, a one-shot Alembic upgrade, the API, and `apex-worker` start together. The worker reuses `recover_available`. It does not contain a second goal runner or a second MCP client. `APEX_DEFER_GOAL_EXECUTION` makes the API persist a goal at zero waves so the worker is what runs it. The development Compose file remains PostgreSQL only, on a different port and volume.

Worker health is a small HTTP server in the worker process. `/health` is 200 only after a poll that could query the database. SIGTERM finishes the current pass and releases the lease. SIGKILL relies on lease expiry. A second worker is safe because the lease update is still the only coordinator.

Memory deletion stays the purge of `memories`, `memory_revisions`, and `memory_events`, plus a content-free tombstone. `context_records` are redacted for a case- and whitespace-normalized copy of a deleted string of at least 12 characters. Task, execution, approval, goal, and trusted context rows are operational audit and are not deleted to satisfy a memory purge. `APEX_OPERATIONAL_RETENTION_DAYS` defaults to 0. A positive value expires old untrusted context proposals only. Backups and WAL stay operator-controlled.

No schema revision was added. The existing columns are enough.

## Consequences

Restarting the API does not resume goals. Restarting, or leaving running, the worker does. An operator who runs only `compose.yaml` still has to start `apex-worker` themselves. Production authentication is unchanged: the image refuses development auth, and `/ready` stays 503 until identity is configured. Live Vault and live OIDC remain deferred. Two workers do not require Redis. Purge must not be documented as erasure of operational records or of backups.
