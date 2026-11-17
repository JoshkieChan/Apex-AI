# ADR 0015 — Governed connectors

## Status

Accepted.

## Decision

External systems are connections owned by an Apex principal. A connection is not a principal, a grant, or an agent. MCP remains the remote tool path and still uses `McpRegistrationService`. API and device reference operations are an allowlist in `apex.connectors.catalog`. There is no generic HTTP adapter.

Discovered operations stay unavailable until a registrar accepts them. Apex assigns the risk and permission from its catalog, including the fixture MCP tools. A connector, a model, or a client field cannot lower either one. An unknown tool stays unavailable. Accepting an operation stores it. The API and the worker install the same accepted operations from those rows. `ExecutionEngine` still authorizes and runs them. Before a call, the adapter uses the task project and refreshes the connection ceiling from the committed row when that database is reachable. A process-local gate is only the fallback when the row cannot be read, which is the isolated test database. A server key is stored once per owner. Health evidence updates timestamps and can mark an active connection degraded. It does not make a disconnected, disabled, or revoked connection active again. The device owner can rotate a lost pairing code. The new code is returned once, the previous code stops working, and only the hash is stored.

Credentials are `env:`, `file:`, or `vault:` references. The reference API and device do not take a secret. Responses do not include secret values or pairing-code hashes. Revocation and disablement leave the rows in place and disable the capability. A non-idempotent note that fails after the write stays uncertain and is not replayed by the worker.

The reference inventory and reference device are in-process fixtures. They are the tested integration. A live third-party account is not required and was not called.

## Consequences

Schema revision `0008_connectors` adds `connections`, `connection_operations`, and `devices`. OAuth is not implemented because no current connector needs it. Adding a connector that needs OAuth requires authorization-code security, PKCE where applicable, and secret-provider storage. It must not be a generic token framework with no consumer.
