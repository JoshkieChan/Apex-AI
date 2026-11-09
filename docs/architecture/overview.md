# Architecture overview

Apex in this milestone is one Python process with a recovery poller. The API accepts a request, and `apex.runtime.Orchestrator` moves the task through interpretation, planning, authority, execution, and verification.

## Components

| Module | Responsibility |
| --- | --- |
| `apex.api` | HTTP contracts, bearer identity, error shape |
| `apex.runtime` | State machine, leases, task view |
| `apex.intent` | Deterministic classification. Model drafts are advisory |
| `apex.planning` | Ordered steps, dependency checks, registry match |
| `apex.agents` | In-process helpers for a single task: Coordinator, Planner, Specialist, and Verifier |
| `apex.coordination` | Logical agents, goal graphs, scheduling, and PostgreSQL goal leases. Roles do not grant authority |
| `apex.authority` | Default-deny grants, one-hop delegation, and approval records |
| `apex.capabilities` | The only map from a capability id to an adapter |
| `apex.execution` | Schema, authority, timeout, and execution record |
| `apex.verification` | Deterministic checks and claim assessment. Missing evidence is not a pass |
| `apex.memory` | Governed memory, provenance, retrieval, and context assembly. Proposals stay untrusted |
| `apex.providers` | Mock, OpenAI, and Anthropic structured-output adapters |
| `apex.mcp` | Fixture client, explicit registration, discovery review |
| `apex.persistence` | SQLAlchemy models, repositories, Alembic metadata |

## Dependency direction

`contracts` has no engine imports. `api` depends on `runtime` and does not call adapters. `intent` and `planning` cannot execute. `execution` re-reads authority and the registry. `verification` does not import execution. Provider modules do not decide permissions.

The logical agents are function objects in-process. The Specialist can only call `ExecutionEngine`. The Verifier can attach a model note and cannot change a failed check into a pass.

## What is intentionally one package

Eleven installable packages would add workspace machinery without a second deployable service. Boundaries are modules plus `tests/unit/test_support_modules.py`, which fails if the API imports the execution engine or a runtime module imports `subprocess` or `socket`.

## Extension points

- Register another `CapabilitySpec` with an adapter and a verification kind.
- Add a provider that implements `complete_structured`.
- A registrar turns a discovered fixture tool into a `CapabilitySpec`. Discovery does not do that.
- A2A is not implemented.

See [runtime lifecycle](runtime-lifecycle.md) and [ADR 0001](../decisions/0001-single-package.md).
