# ADR 0005 — MCP is a review boundary, not a client

## Status

Accepted for the review helper. The "does not speak MCP" consequence is superseded by ADR 0007 for the in-process fixture. Discovery still does not register or execute a tool.

## Decision

This milestone defines tool descriptors, a static source, and a review function that refuses automatic registration. It does not speak MCP.

## Why

A partial JSON-RPC client that executes every listed tool would violate the registry rule and would be easy to mistake for support. The execution path already accepts only registered adapters, so a later MCP adapter can sit behind that path.

## Consequences

Documentation and the status page must keep saying that MCP interoperability is remaining work. A2A is out of scope until a separate decision.
