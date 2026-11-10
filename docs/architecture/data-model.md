# Data model

PostgreSQL is the local development database. Tests create the same SQLAlchemy metadata on SQLite. Alembic revisions `0001_initial` through `0008_connectors` are the schema source for a real database. Application startup does not call `create_all`. Do not edit an applied revision; add a new one. Production readiness requires `0008_connectors`. `0006_reliability` adds goal lease columns, `goal_verifications`, and `memory_purges`. `0007_interaction` adds `conversations`, `interactions`, and `media_objects`. It downgrades by dropping those three tables. `0008_connectors` adds `connections`, `connection_operations`, and `devices`. It downgrades by dropping those three tables. Secret values and pairing codes are not columns. Raw image and audio bytes are not columns.

## Tables

- `principals` — development identities. `external_key` is unique.
- `permission_grants` — one effect per principal and permission key: `allow`, `deny`, or `require_approval`. `resource_scope` defaults to `*`. `expires_at` and `revoked_at` remove the grant from evaluation.
- `identity_subjects` — unique `(issuer, subject)` mapped to one principal.
- `delegations` — one-hop permission copy. The delegator and delegate must differ.
- `tasks` — lifecycle, request text, outcome, error, lease, and idempotency key. Unique on `(principal_id, idempotency_key)` when the key is present. SQL treats null keys as distinct, so requests without a key are not collapsed.
- `task_events` — append-only transitions and role events.
- `plans` and `plan_steps` — versioned plan, ordered steps, payloads, bindings, dependency lists, and operation hashes.
- `approvals` — pending, approved, rejected, expired, or cancelled. Unique on `(plan_step_id, operation_hash)`. `consumed_at` is set when an attempt is inserted.
- `executions` — one row per attempt. Unique on `(plan_step_id, attempt)`. `certainty` is `definite` or `uncertain`. Status includes `uncertain`.
- `mcp_servers`, `mcp_tools`, `capability_versions`, `registration_events` — fixture trust and registration. No secret columns.
- `verifications` — one row per execution. Status is `passed`, `failed`, or `inconclusive`.
- `context_records` — task outcome log. `trust` is `proposal` or `trusted`. This is not the governed memory table. Purge redacts a normalized copy of deleted memory text of at least 12 characters and keeps the row. Short strings, hashes, and paraphrases are not removed. Untrusted proposals can expire when `APEX_OPERATIONAL_RETENTION_DAYS` is positive. Trusted rows stay.
- `memories`, `memory_revisions`, `memory_events` — current memory, immutable versions, and audit events. Audit detail does not include content. `mcp_tools.reconciles_capability_id` names a candidate reconciliation read and does not make a remote response independent evidence.
- `logical_agents` — role definitions and lifecycle. `allowed_permissions` is an upper bound, not a grant.
- `goals`, `goal_subtasks`, `agent_invocations`, `plan_revisions`, `goal_events`, `goal_verifications` — a coordinated objective, its graph, one row per agent attempt, each accepted revision, the coordination audit, and the verification decision for a plan revision. `goals` also stores `lease_owner`, `lease_expires_at`, and `recovery_attempts`. Invocation payloads store memory ids, not memory content. Verification evidence stores check names and the revision, not secret values.
- `memory_purges` — tombstones for physically removed memories. Columns are the memory id, scope, revision count, and time. There is no content column.
- `conversations` — one multimodal session for one principal. `project_key` is optional. This is not a memory.
- `interactions` — one turn. `request_json` and `response_json` store the normalized contract. Status is `completed` or `failed`. A reserved turn uses `failed` with `error_code` `in_progress` until the provider returns. Unique on `(principal_id, idempotency_key)` when the key is present.
- `media_objects` — metadata for a temporary file: kind, media type, size, digest, dimensions or duration, provenance, trust, expiry, and deletion time. `storage_key` is a generated 32-character hex name. The file is not in the database.

Foreign keys use `ON DELETE RESTRICT`. Status columns have check constraints. JSON columns store validated payloads: intent, step input, execution input and output, evidence, and context content. They are not a substitute for the task status or the principal.

Timestamps are timezone-aware UTC. `UtcDateTime` stores them as UTC and returns aware values, including from SQLite, which has no timestamptz type.

`uncertain` can move only to `completed` or `failed`, and only through reconciliation. The worker does not retry it. Revisions can be downgraded on an empty database. Downgrade is not a production data migration.
