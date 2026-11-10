# ADR 0015 — Governed connectors

## Status

Accepted.

## Decision

External systems are connections owned by an Apex principal. A connection is not a principal, a grant, or an agent. MCP remains the remote tool path and still uses `McpRegistrationService`. API and device reference operations are an allowlist in `apex.connectors.catalog`. There is no generic HTTP adapter.

Discovered operations stay unavailable until a registrar accepts them. Apex assigns the risk and permission from that catalog. A connector cannot lower either one. Accepting an operation installs a capability in the existing registry. `ExecutionEngine` still authorizes and runs it. A process-local gate records the connection state, scopes, project, and expiry, and the adapter checks that gate before it touches reference state. Legacy MCP servers with no connection row keep their existing checks.

Credentials are `env:`, `file:`, or `vault:` references. The reference API and device do not take a secret. Responses do not include secret values or pairing-code hashes. Revocation and disablement leave the rows in place and disable the capability. A non-idempotent note that fails after the write stays uncertain and is not replayed by the worker.

The reference inventory and reference device are in-process fixtures. They are the tested integration. A live third-party account is not required and was not called.

## Consequences

Schema revision `0008_connectors` adds `connections`, `connection_operations`, and `devices`. OAuth is not implemented because no current connector needs it. Adding a connector that needs OAuth requires authorization-code security, PKCE where applicable, and secret-provider storage. It must not be a generic token framework with no consumer.
