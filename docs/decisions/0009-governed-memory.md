# Governed memory stays in PostgreSQL

Apex memory is relational. `memories` holds the current record. `memory_revisions` holds each version. `memory_events` records the mutation without the content. Scopes are `personal`, `project`, `task`, and `system`. Reliability (`unverified`, `observed`, `inferred`, `verified`, `disputed`, `superseded`) is separate from lifecycle (`candidate`, `active`, `superseded`, `expired`, `deleted`).

`context_records` remains the task outcome log from earlier milestones. It is not the governed memory model. A passed verification also writes one task-scoped `verified_outcome`. That write is produced by the runtime after deterministic verification, not by the model.

Retrieval ranks scope, text overlap, reliability, recency, and constraints. It does not use embeddings. Semantic similarity is not treated as truth. Authorization runs before a row can enter the result. Context assembly puts a fixed instruction first and the selected rows in a `MEMORY_DATA` payload. Providers receive that contract. They do not query the database.

Model proposals stay `inferred` candidates. Verification accepts a source user's confirmation, a passed `mcp.fixture.records_lookup` execution, or a passed verification row. A `require_approval` grant does not perform the mutation.

Deletion is logical. Rows, revisions, and events remain. Production readiness for this revision is `0004_memory`. API processes still do not migrate on startup.

## Consequences

- A second database or `pgvector` is not required for this milestone.
- Remote MCP reconciliation is still inconclusive when the only evidence would be the remote server's own response.
- Env secret references remain the only secret provider. Requiring an external provider in production fails startup.
- Development token issuance rejects proxy headers and still must not be exposed through a shared proxy.
