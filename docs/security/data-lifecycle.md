# Data lifecycle

Four behaviors are separate. None of them stands in for the others.

1. **Governed-memory deletion.** Logical delete, then physical purge of the memory, its revisions, and its events. The tombstone has no content. Search and later context assembly do not return the memory.
2. **Derived-context redaction.** A limited edit of `context_records` during that purge.
3. **Operational audit retention.** Task, plan, approval, execution, goal, and invocation rows stay so authorization and execution can still be explained.
4. **Infrastructure retention.** Backups, WAL, and snapshots are outside this process.

## Categories

| Category | Original user text | Derived text | Points at memory | Can rebuild deleted text | Removed by memory purge | Why it remains | Physical erasure |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `memories` | yes | no | it is the memory | yes, until purge | yes, after the retention window | only until `APEX_MEMORY_PURGE_AFTER_HOURS` | yes, the row |
| `memory_revisions` | yes | no | memory id | yes, until purge | yes | no | yes, the row |
| `memory_events` | no content column | event detail without content | memory id | no | yes | no | yes, the row |
| `memory_purges` | no | id, scope, revision count, time | memory id | no | the tombstone is the record of purge | audit that a purge happened | not by purge |
| assembled context | copies of current memory | ranked for one call | memory ids | only while the memory is current | not stored as its own table | the response is not persisted | n/a |
| `context_records` | may quote memory or the task | outcome JSON | sometimes, inside JSON | only for text the redaction matches | no, the row stays | task outcome audit | redaction of a matching string, or proposal expiry |
| `tasks` and `task_events` | `request_text` | intent and result JSON | no | yes, from `request_text` or payloads | no | who asked for what, and the lifecycle | not by memory purge |
| `plans`, `plan_steps`, `plan_revisions` | step input | operation hash and bindings | no | yes, from step payloads | no | the plan that was authorized | not by memory purge |
| `approvals` | canonical payload | hash and bindings | no | yes, from the payload | no | the approval must stay checkable | not by memory purge |
| `executions` and `verifications` | input payload | output, evidence, summary | no | yes, from input or output | no | execution and verification history | not by memory purge |
| `goals` and `goal_subtasks` | `original_request` and inputs | outputs and status | no | yes, from those columns | no | goal recovery and the coordination audit | not by memory purge |
| `agent_invocations` | no memory body | memory ids and the agent output | memory ids only | the id, not the body | no | which attempt ran | not by memory purge |
| `goal_events` | no | event names and small payloads | goal id | no memory body | no | coordination audit | not by memory purge |
| logs | should not include the body | instance, phase, counts, exception type | no | no, when the formatter is used | no | operations | process output, not a table |
| backups and WAL | whatever was committed | the same | the same | yes, until the operator expires them | no | disaster recovery | operator retention only |

A table is not harmless because its name is not `memories`. `tasks.request_text`, approval payloads, and execution inputs are user or derived text. Purge does not delete them. A deleted memory can still be read from those rows. This document does not claim that reconstruction is impossible.

## Governed memory

`DELETE /v1/memory/{id}` sets `lifecycle` to `deleted`. Search and `POST /v1/memory/context` skip that row. `POST /v1/memory/purge` and the worker remove a deleted row whose `deleted_at` is older than `APEX_MEMORY_PURGE_AFTER_HOURS`. Revisions and events for that id go with it. `memory_purges` stores the id, scope, revision count, and time. Running purge again does not remove the tombstone and does not return the memory. The same result is visible through a new database connection.

An active or completed goal does not block that purge. The goal row remains. Invocation input keeps memory ids and does not keep the memory body.

## Derived context

Redaction runs on `context_records.content` for each purged memory. The deleted string is compared after Unicode case folding and after collapsing whitespace to single spaces. A match requires at least 12 characters in that normalized form. The matched span, including the original case and spacing, is replaced with `[redacted]`. The row stays.

This does not match:

- a secret shorter than 12 normalized characters
- a paraphrase
- a hash or other encoding of the secret
- a suffix or prefix that is not the whole secret
- text that merely means the same thing

Case differences and extra spaces between the same words do match.

## Operational audit

`APEX_OPERATIONAL_RETENTION_DAYS` defaults to 0. Zero means Apex does not expire operational rows.

When the value is positive, the worker deletes `context_records` whose `trust` is `proposal` and whose `created_at` is older than that many days. It does not delete:

- `trust` `trusted` context rows
- tasks, task events, plans, plan steps, or plan revisions
- approvals
- executions or verifications
- goals, subtasks, invocations, or goal events

Those rows are how an operator checks who approved an operation, what was executed, and which uncertain outcome is still unresolved. Deleting them would make the authority record weaker. Proposal expiry does not run for a row that is an approval, an execution, or a goal, because those are not context proposals.

## Infrastructure

Apex cannot delete a row from an immutable backup, a base backup, a WAL archive, or a snapshot taken outside this database. `APEX_BACKUP_RETENTION_DAYS` records the retention the operator is expected to apply. It does not expire a backup. Erasure from those copies is an operator and infrastructure action, and this repository does not verify it.
