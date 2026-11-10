# Operator runbooks

These procedures match the supported deployment: one API, one worker, and PostgreSQL 16. They assume the operator can read container logs and `psql`. They do not assume Kubernetes.

A new capability attempt is stopped with `POST /v1/operations/execution` and `{"suspended": true}` by a principal that holds `operations.suspend`. That does not mark an uncertain execution failed and does not send it again.

## Database unavailable

Symptoms: API `/health` reports `database: unavailable`. Worker `/health` returns 503 and the log says the database poll failed. `/ready` is 503.

Impact: no new tasks are stored and the worker does not claim goals.

Checks: `pg_isready` against the database container. Confirm the API and worker use the same `APEX_DATABASE_URL` host, and that the published port is not the development port unless that is intentional.

Action: restore database connectivity. Do not delete `apex_pg_data` or `docker_apex_pg_data` to make the error stop. When `pg_isready` succeeds, the next worker poll continues from the existing rows. Confirm `/health` shows `database: ok` and, once authentication is configured, `/ready` is 200.

Escalate when the volume is missing or PostgreSQL will not start after the data directory is intact.

## Migration failure

Symptoms: the `migrate` container exits non-zero. API `/ready` stays 503 in production because `alembic_version` is not `0009_controls`.

Impact: the new process must not serve traffic against a schema it does not understand.

Checks: read the migrate logs for the revision it stopped on. `SELECT version_num FROM alembic_version`.

Action: leave the API and worker stopped. Fix the failed revision in a new migration if the database is at an older revision. Do not edit an applied revision file. If the upgrade partially applied, restore the dump from `docs/operations/backup.md` into a new volume and run `alembic upgrade head` once. Confirm the version, then start one API and one worker.

Escalate when the only copy of the data is the volume the failed migration was writing.

## Worker failure

Symptoms: worker `/health` is 503, or the process has exited. Goals stay `planned` or `running` with an expired lease.

Impact: deferred goals are not resumed until a worker is running. An unexpired lease is not taken by a second process.

Checks: `docker logs` for the worker. The health body separates poll failures from recovery failures.

Action: start one worker. Do not start a second worker beside a live one. SIGTERM lets the current pass finish and release its lease. SIGKILL leaves the lease until `APEX_LEASE_SECONDS`. After expiry, one worker may resume. A succeeded step is not executed again. An execution with `possibly_sent` stays uncertain.

Escalate when the same goal reaches `recovery_exhausted`.

## Provider outage

Symptoms: interactions or advisory model calls fail with `unavailable`. Tasks that do not call a model still use the execution engine.

Impact: the configured provider is not replaced. User content is not sent to a different provider.

Checks: `APEX_MODEL_PROVIDER` and the modality flags. The mock provider is the fixture. A commercial adapter fails when its key or model name is empty.

Action: restore the configured provider's credential in the secret provider, or set `APEX_MODEL_PROVIDER` to a provider the operator has approved and restart the API. Record the change. Do not point a production process at a second provider to hide the outage.

Escalate when the credential itself is exposed. Treat that as a credential compromise.

## Provider credential compromise

Symptoms: a provider key appears in a log, a ticket, or a repository file.

Impact: the provider can be called by whoever holds the key. Apex grants are unchanged until an operator changes them.

Checks: search logs for the key prefix. Confirm `.env` is not in git history for this incident. Do not print the key in the incident note.

Action: revoke the key at the provider. Put the replacement in `env`, `file`, or `vault` under a new name. Restart the API and worker. Set the execution hold if calls continued after the exposure. Confirm a task no longer fails with the old authentication error.

Escalate when the key was committed. Do not rewrite published git history unless the operator authorizes that separately.

## MCP endpoint compromise

Symptoms: a registered remote server returns unexpected tools, or its host is no longer the one that was reviewed.

Impact: discovered tools stay unavailable until a registrar accepts them. A previously accepted tool can still be invoked until it is disabled.

Checks: `GET /v1/mcp/tools` and the connection row. Compare the endpoint with the reviewed value.

Action: `POST /v1/connections/{id}/revoke` or disable the tool. Set the execution hold if invocations may still be in flight. An uncertain `records_update` stays uncertain. Do not register the new schema to "get it working" before reading it.

Escalate when the server holds a credential that was sent as a secret reference. Rotate that secret.

## Connector credential expiration

Symptoms: execution returns `credential_expired` or `connection_unavailable` before dispatch. `outcome_certainty` stays unset because no execution row is created for that attempt.

Impact: new work on that connection stops. Other connections are unchanged.

Checks: the connection `expires_at`, `state`, and `secret_configured`. The API does not return the secret.

Action: store a new secret reference and authorize the connection again. Do not put the secret in the connection row. Resume only the operations that never dispatched. Leave uncertain ones for reconciliation.

## Uncertain external write

Symptoms: execution status `uncertain`, `outcome_certainty` `possibly_sent`, goal status `uncertain`.

Impact: the write may have happened. Recovery, `/run`, and replan do not send it again.

Checks: `GET /v1/executions/{id}/reconciliation`. The in-process fixture can be read back. A remote server without that read stays inconclusive.

Action: a principal with `operations.reconcile` calls `POST /v1/executions/{id}/reconcile` once. If the result is inconclusive, reconcile with the external system by hand and do not submit the same non-idempotent write again.

Escalate when two execution rows exist for one non-idempotent step. That is a defect, not a reason to retry.

## Approval failure

Symptoms: a high-risk task stays `awaiting_approval`, or `decide` returns `approval_mismatch`, `approval_expired`, or `separation_of_duties`.

Impact: the adapter has not run for that pending approval.

Checks: the approval's capability id, operation hash, and requester. The requester cannot approve their own high-risk operation.

Action: a different principal with `authority.approve` decides the original approval. If the payload changed, create a new task. Do not edit the stored hash to match a new payload.

## Memory incident

Symptoms: context contains a fact that should not be current, or a purge did not remove a deleted memory that is past the retention window.

Impact: memory does not grant permission. A wrong fact can still be shown to a model as `MEMORY_DATA`.

Checks: `GET /v1/memory/{id}/history`, `lifecycle`, and scope. Search and context skip deleted rows.

Action: dispute or supersede the current version, or delete it. Purge with `memory.manage` only removes deletions older than `APEX_MEMORY_PURGE_AFTER_HOURS`. It leaves task, execution, approval, and goal rows. It does not rewrite a backup.

## Media failure

Symptoms: upload returns 400 or 413, or an interaction fails before the model call.

Impact: the turn does not execute a tool because of text inside the file. Request-scoped bytes are deleted when the turn finishes, including failure.

Checks: the media type, declared size, and owner. Another principal's media is 404.

Action: submit a supported PNG, JPEG, WebP, WAV, or MPEG-1 Layer III file within the size limit. Do not accept GIF, WebM, or M4A by widening the parser during an incident.

## Resource pressure

Symptoms: requests time out, the worker poll falls behind, or logs show repeated `retry_limit` or adapter timeouts.

Impact: idempotent timeouts are definitive failures. Non-idempotent timeouts after a possible send are uncertain.

Checks: `/health` counts for recoverable goals, uncertain operations, and approvals. Worker health counters.

Action: set the execution hold to stop new attempts. Let the in-flight attempt finish or hit its timeout. Do not raise `max_attempts` on a non-idempotent capability to clear the queue. Restart one worker only after the hold is in place if the process is wedged.

## Security incident

Symptoms: unexpected grants, a connector registering itself, or a token used from an unknown client.

Impact: a bearer token is the identity. A model response cannot add a grant.

Checks: grants for the principal, connection state, and whether development issuance is enabled. Production startup refuses development issuance.

Action: revoke the grants. Revoke the connection. Set the execution hold. Rotate the affected signing key or provider secret. Confirm `/v1/dev/tokens` is 404 in production. Resume execution only after the grants you intend are the ones that remain.

## Failed deployment

Symptoms: Compose does not become healthy, or `/ready` stays 503 after migrate succeeded.

Checks: authentication environment variables. An empty production auth configuration stays alive and not ready. `APEX_POSTGRES_PASSWORD` must be set. PostgreSQL is not published by the deploy file.

Action: read the migrate and API logs. Do not switch the deploy volume to `docker_apex_pg_data`. Fix the configuration and start again. Use the rollback in `docs/release/v1.0.md` if the new image should not stay.

## Restore

Follow `docs/operations/backup.md`. After restore, read one pre-dump task and one uncertain execution before accepting traffic.
