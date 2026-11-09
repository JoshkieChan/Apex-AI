# ADR 0004 — Synchronous execution with lease recovery

## Status

Accepted

## Decision

`POST /v1/tasks` runs the state machine in the request until the task completes, fails, or waits for approval. A worker command resumes rows whose lease expired. There is no Redis, Celery, or external orchestrator.

## Why

The first slice needs idempotency, crash recovery, and approval pauses. A queue would add a failure domain before there is a second worker. The code does not describe itself as highly available.

## Consequences

A request blocks for the adapter timeout. Clients that need a quick accept can be added later by returning after `submitted` and letting the worker advance; the states already allow that. It is not the current API behavior.
