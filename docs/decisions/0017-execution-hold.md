# ADR 0017 — Execution hold

## Status

Accepted.

## Decision

An operator with `operations.suspend` can set `runtime_controls.execution_suspended`. The hold update and the start of a new attempt lock that row. The lock is the linearization point. An attempt that commits its reservation before the hold gets the lock is already in flight: a completed row is returned, an uncertain row is not replayed, and a running row yields. Once the hold commit returns, a later attempt sees the lock, raises `execution_suspended` with `dispatched` false, and does not insert an execution row or consume an approval.

The hold does not delete rows, does not mark an uncertain execution failed, and does not replay it. Lifting the hold does not resume a task that already failed while the hold was on. The caller submits that work again.

`operations.suspend` is a control-plane permission. It cannot be delegated, and a connector cannot register a tool with that permission.

## Consequences

Schema revision `0009_controls` adds `runtime_controls`. A missing row means execution is open. The supported deployment remains one API process and one worker. Both read the same table.
