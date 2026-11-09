# ADR 0002 — Deterministic authority

## Status

Accepted

## Decision

The deterministic interpreter chooses capabilities. Grants in `permission_grants` choose allow, deny, or approval. The execution engine evaluates grants again. Model JSON cannot contain an authorization field that the runtime honors. Verification status comes from deterministic checks.

## Why

A model that can name tools and also decide that approval is unnecessary can expand its own authority. Keeping those decisions in code and in grant rows makes tests deterministic and keeps paid APIs out of the suite.

## Consequences

Paraphrases do not execute. They return a clarification. Wider language coverage has to stay behind the same registry and grant checks.
