# ADR 0007 — Register MCP tools through the execution engine

## Status

Accepted. Supersedes the "does not speak MCP" consequence of ADR 0005. Discovery still does not register or execute a tool.

## Decision

The local fixture is an in-process MCP server from the `mcp` 2.x SDK. An authorized principal discovers tools, then registers each one with an Apex-assigned risk, permission, and verification kind. The adapter is installed in `CapabilityRegistry` and runs only from `ExecutionEngine`. A dropped update response is an uncertain terminal outcome.

## Why

A custom JSON-RPC client would drift from the protocol. A remote transport is unnecessary for the deterministic fixture and would violate the adapter rule against unrestricted network access. In-process `Client(server)` uses the SDK and keeps the fixture off the host filesystem and shell.

## Consequences

Only the server key `fixture` can be added. Other transports remain deferred. ADR 0005's review helper remains for unregistered descriptors and still refuses automatic registration.
