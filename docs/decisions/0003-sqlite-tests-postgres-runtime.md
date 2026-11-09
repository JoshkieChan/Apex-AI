# ADR 0003 — SQLite tests, PostgreSQL for local runtime

## Status

Accepted

## Decision

Models use portable types: string UUIDs, JSON, and timezone-aware datetimes. The default test suite applies those models on SQLite. Alembic targets the same metadata and is applied to SQLite in tests. PostgreSQL runs from Docker Compose for local development and in CI when `APEX_POSTGRES_TEST_URL` is set.

## Why

Requiring Docker for every unit test makes the suite brittle. A migration that only runs on one engine would drift. The PostgreSQL check is real when the URL is present and is not pretended when it is absent.

## Consequences

PostgreSQL-only features such as partial unique indexes and `FOR UPDATE SKIP LOCKED` are not used yet. Lease claims are conditional updates. That is an honest single-database lock, not a claim of cluster safety.
