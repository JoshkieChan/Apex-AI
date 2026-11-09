# ADR 0001 — One package, explicit modules

## Status

Accepted

## Decision

Apex ships as the `apex` package under `src/apex`. API and worker processes are thin entry points. The runtime is not a separate network service in this milestone.

## Why

The requested tree showed many packages so responsibilities stay visible. Installing eleven packages, with a workspace tool and cross-package version pins, would not make the authority boundary more real. Module imports and a boundary test do.

A always-on `apex-runtime` process without a queue would look distributed and would not be. Recovery is `apex-worker`, which polls the same database.

## Consequences

New capabilities are registrations in `apex.capabilities`, not new services. A later split is still possible if a worker must deploy separately; the orchestrator is already the seam.
