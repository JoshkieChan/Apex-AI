# ADR 0017 — Execution hold

## Status

Accepted.

## Decision

An operator with `operations.suspend` can set `runtime_controls.execution_suspended`. `ExecutionEngine` reads that row after it has returned a completed execution, refused an uncertain retry, and yielded to an in-flight attempt. A new attempt raises `execution_suspended` with `dispatched` false before an execution row is inserted and before an approval is consumed.

The hold does not delete rows, does not mark an uncertain execution failed, and does not replay it. Lifting the hold does not resume a task that already failed while the hold was on. The caller submits that work again.

`operations.suspend` is a control-plane permission. It cannot be delegated, and a connector cannot register a tool with that permission.

## Consequences

Schema revision `0009_controls` adds `runtime_controls`. A missing row means execution is open. The supported deployment remains one API process and one worker. Both read the same table.
