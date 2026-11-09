# Runtime lifecycle

`Orchestrator.submit` inserts a `submitted` task. If the principal and idempotency key already exist, it returns that task and does not run it again.

`advance` claims a lease for `APEX_INSTANCE_ID`. The claim is a conditional update, so a second process does not take a live lease. The lease is cleared when the call returns. A crash leaves the lease until `APEX_LEASE_SECONDS` passes. `apex-worker` then resumes tasks with an expired lease, up to `APEX_MAX_TASK_ATTEMPTS`. The same process resumes `planned` and `running` goals. The supported Compose file runs one worker continuously. Horizontal scaling is unsupported: a live owner is not fenced after the lease expires. SIGTERM stops new polls and waits up to `APEX_WORKER_SHUTDOWN_SECONDS` for the current pass. If the pass does not finish, the process exits and abandons the adapter thread. Python does not cancel that thread. The lease then expires. SIGKILL leaves the lease until it expires. Docker sends SIGKILL after `stop_grace_period` (25 seconds) if the process is still alive.

## States

| From | To |
| --- | --- |
| submitted | interpreting, failed, cancelled |
| interpreting | planned, completed (clarification only), failed, cancelled |
| planned | awaiting_approval, executing, failed, cancelled |
| awaiting_approval | executing, failed, cancelled |
| executing | awaiting_approval, verifying, failed, cancelled, uncertain |
| verifying | completed, failed, uncertain |

`completed`, `failed`, `cancelled`, and `uncertain` are terminal. `uncertain` means a non-idempotent call may have changed external state and must not be retried automatically.

Cancellation is accepted only before execution starts. It is not a preemptive kill of a running adapter.

## What each phase does

1. **Interpreting.** The deterministic interpreter selects capabilities and typed inputs. The Coordinator may ask the model for a note. Unknown capability names are stored as ignored suggestions. If nothing matches, the task completes as `clarification` with no plan and no execution.
2. **Planning.** Steps are contiguous, dependencies point at earlier steps, and each capability must be registered and available. Inputs are schema-checked. Bindings are a separate map, not a `$ref` inside user JSON.
3. **Authority.** Every incomplete step is evaluated. Missing or explicit `deny` fails the task. `require_approval` creates a pending approval bound to the operation hash, payload, and bindings. No step runs while a required approval is pending.
4. **Executing.** The Specialist calls the execution engine, which checks the registry, schema version, recomputes the hash, re-evaluates authority, validates the resolved input, enforces the timeout, and writes an execution row. A succeeded row for that step is returned instead of running the adapter again. A non-idempotent timeout, and a remote failure after the tool request was sent, persist `uncertain` / `possibly_sent`. A remote connection failure before that request is `not_sent` and is a normal failure. The task and, for a goal, the subtask follow the persisted row. The execution id is kept.
5. **Verifying.** Capability checks recompute the expected word count, export receipt, or fixture record. The task reaches `completed` only when every check passed. An inconclusive check becomes `uncertain`. A trusted task-scoped context record is written only after a pass. Proposals are not written automatically.

An approval decision resumes the original task in the same request when the decision is approve. A rejection fails the task. The approver must hold `authority.approve` and must not be the requester.

## Failure and retries

Idempotent adapter timeouts fail the task and may be retried within the attempt limit. `text.analyze` and `records_lookup` allow two attempts. `records.export` and `records_update` allow one attempt. A non-idempotent timeout is `uncertain` and is not retried. This is single-process recovery, not a distributed transaction log, and it is not exactly-once execution.
