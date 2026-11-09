# 0013. Execution certainty and deployment limits

A non-idempotent timeout was persisted as an uncertain execution and then reported as a failed goal, because coordination trusted the exception code. The deployment file also shipped a database password and published services as if that Compose project were an internet deployment. The lease was described as multi-worker safety even though a live owner is not fenced after expiry.

## Decision

Persisted execution status and `outcome_certainty` decide the coordination outcome. `possibly_sent` or execution status `uncertain` makes the subtask and the goal `uncertain`, keeps the execution id, and blocks recovery, `/run`, and replan from calling the adapter again. The original timeout code stays on the execution row for diagnosis. An idempotent timeout stays `timed_out` with `inconclusive` and can fail the goal. A validation failure before an execution row exists is a normal failure.

`compose.deploy.yml` requires `APEX_POSTGRES_PASSWORD` and has no default. It does not publish PostgreSQL or worker health. The API port is `127.0.0.1`. That file is a local production-mode scaffold. TLS, a reverse proxy, and an external identity configuration are required before the API is reachable beyond the host. Development authentication stays off.

The supported topology is one worker. The lease still stops a second process from taking an unexpired claim, and recovery after process death still works. There is no heartbeat and no fencing token. A worker that continues after its lease expires can still commit. Horizontal scaling is unsupported. Adding a fence would have to cover the execution commit inside `ExecutionEngine`, which is a new ownership protocol rather than a proven extension of the current lease. This pass does not add that protocol or a schema revision.

Proposal expiry skips a proposal whose scope id is an unresolved task, an unresolved goal, or the task id of an unresolved goal. Redaction normalizes with Unicode case folding, collapses whitespace, applies the length threshold to the normalized secret, and replaces the original source span.

Worker `/health` is 200 only while the last successful poll is fresh. Claimed work is not a successful recovery. `APEX_WORKER_SHUTDOWN_SECONDS` is the deadline after SIGTERM. Compose `stop_grace_period` is longer than that deadline. A pass that misses the deadline is abandoned and its lease expires. That is not a promise to finish arbitrary in-flight work.

## Consequences

M5.2 is not formally accepted by this record. M6 stays on hold. Vault and live OIDC stay deferred. Schema revision remains `0006_reliability`.
