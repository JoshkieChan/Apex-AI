# 0013. Execution certainty and deployment limits

A non-idempotent timeout was persisted as an uncertain execution and then reported as a failed goal, because coordination trusted the exception code. The deployment file also shipped a database password and published services as if that Compose project were an internet deployment. The lease was described as multi-worker safety even though a live owner is not fenced after expiry.

## Decision

Persisted execution status and `outcome_certainty` decide the coordination outcome. `possibly_sent` or execution status `uncertain` makes the subtask and the goal `uncertain`, keeps the execution id, and blocks recovery, `/run`, and replan from calling the adapter again. The original error code stays on the execution row for diagnosis. A remote MCP failure carries whether the `tools/call` request was handed to the network. A connection, pool, or TLS failure before that request is `not_sent` and is a normal failure. A read, write, or protocol failure after it, for a non-idempotent tool, is `possibly_sent`. The httpx transport retries are zero, and Apex does not send that request again. An idempotent remote timeout stays a definitive failure. An idempotent local timeout stays `timed_out` with `inconclusive`. A validation failure before an execution row exists is a normal failure.

Repair of a running subtask looks up the plan step's execution when the subtask does not yet store the execution id. A `possibly_sent` row becomes uncertain and is not replayed. A `not_sent` row that was still running is a normal failure and does not count as an external side effect.

`compose.deploy.yml` requires `APEX_POSTGRES_PASSWORD` and has no default. It does not publish PostgreSQL or worker health. The API port is `127.0.0.1`. That file is a local production-mode scaffold. TLS, a reverse proxy, and an external identity configuration are required before the API is reachable beyond the host. Development authentication stays off.

The supported topology is one worker. The lease still stops a second process from taking an unexpired claim, and recovery after process death still works. There is no heartbeat and no fencing token. A worker that continues after its lease expires can still commit. Horizontal scaling is unsupported. Adding a fence would have to cover the execution commit inside `ExecutionEngine`, which is a new ownership protocol rather than a proven extension of the current lease. This pass does not add that protocol or a schema revision.

Proposal expiry skips a proposal whose scope id is an unresolved task, an unresolved goal, or the task id of an unresolved goal. Redaction normalizes with Unicode case folding, collapses whitespace, applies the length threshold to the normalized secret, and replaces the original source span.

Worker `/health` is 200 only while the last successful poll is fresh. Claimed work is not a successful recovery. A goal counts as claimed only after this process acquires the lease. `poll_failures` counts database poll failures and is separate from task and goal recovery failures. `APEX_WORKER_SHUTDOWN_SECONDS` is the wait after SIGTERM. It is not a promise that Python can cancel an adapter thread. When the wait ends, the process exits and abandons that thread. Compose `stop_grace_period` is 25 seconds and is the SIGKILL backstop. A pass that misses the deadline is abandoned and its lease expires. A `possibly_sent` execution is not replayed after that expiry.

## Consequences

M5.2 is not formally accepted by this record. M6 stays on hold. Vault and live OIDC stay deferred. Schema revision remains `0006_reliability`.
