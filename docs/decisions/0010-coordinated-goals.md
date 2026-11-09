# 0010. Coordinated goals use logical agents

Apex can coordinate a small goal graph without a second authority system or an agent protocol.

## Decision

Complex objectives that match the coordinated utterances become a `goals` row and a directed graph of `goal_subtasks`. The parent `tasks` row attributes the work to the requesting principal and uses the existing task transitions. Its lease stays null, so the recovery worker does not run the graph through the single-task orchestrator.

Logical agents are rows in `logical_agents`. Built-in roles are Coordinator, Planner, Researcher, Executor, and Verifier. Lifecycle is `draft`, `registered`, `active`, `disabled`, or `retired`. Only an active agent can be assigned. Registration does not insert a grant. `HARD_PERMISSIONS` is a ceiling: a stored allowlist can only narrow it. A researcher cannot be assigned a write, and a verifier cannot be assigned a capability.

Every capability subtask is a `plan_steps` row. The scheduler calls `ExecutionEngine.execute` under the requesting principal. The engine still checks the registry, the operation hash, the schema, and authority. High-risk and `require_approval` operations stop in `awaiting_approval`. They are not auto-approved. A denied principal produces no execution row.

The verifier recomputes word counts from the original subtask text. Missing evidence is a failed check. The goal is `completed` only when every current subtask completed and the verifier passed. A revised goal that cancelled a failed agent step and verified the completed work is `partial`. The parent task is then `failed` with `partial_completion`, so a task reader does not treat it as full success.

Replanning refuses any uncertain subtask. It does not reset a completed execution. Cancellation changes pending subtasks to `cancelled` and does not delete executions.

Context comes from `MemoryStore.assemble`. Invocations store memory ids. Model output is not accepted as a graph. Decomposition is deterministic and rejects unknown text. There is no A2A endpoint.

## Consequences

Simple utterances stay on `POST /v1/tasks`. Goal limits are `APEX_MAX_GOAL_SUBTASKS`, `APEX_MAX_PLAN_REVISIONS`, `APEX_MAX_AGENT_INVOCATIONS`, and `APEX_MAX_GOAL_CONCURRENCY`. This decision left the parent task lease null. Milestone 5.1 keeps that and adds a goal lease. See `0011-coordination-reliability.md`.
