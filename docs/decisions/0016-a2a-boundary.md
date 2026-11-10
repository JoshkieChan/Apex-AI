# ADR 0016 — A2A stays a boundary, not a second authority

## Status

Accepted. A2A is not implemented.

## Decision

MCP remains the interoperability mechanism for external tools. Agent-to-agent protocol support would add remote agent identity, delegated task messages, and discovery of agents that are not Apex roles. Those needs are not met by MCP tool calls. They also must not become a second permission system.

If A2A is added later, Apex keeps these responsibilities:

- The Apex principal is the authority context. A remote agent is not a principal merely because it authenticated.
- A remote agent cannot register a connector, store a credential, widen a scope, approve an operation, or mark work verified.
- Messages and tasks cross the existing execution engine. Discovery does not install a capability.
- Delegation cannot exceed the requesting principal or the connection scopes.
- Uncertain external effects stay uncertain. A successful agent message is not verification.
- Audit rows survive revocation. Payloads in logs stay redacted.

What A2A would add, and what this milestone does not build, is a transport and schema for those remote agent messages. Building that transport now would duplicate coordination and authority.

## Consequences

`docs/status.md` continues to list A2A as deferred. A later milestone can add a single reviewed transport behind this boundary. It should not add a network service per role.
