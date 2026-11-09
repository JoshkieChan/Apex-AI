# MCP registration

MCP is an interoperability mechanism. It is not the product. The client is the maintained `mcp` package, version 2.x. The local fixture is an in-process `MCPServer`. Apex does not speak a custom protocol and does not start a subprocess.

`review_discovered_tool` still returns a proposal with `auto_register=false`. That path does not insert a capability. The registration service is the only path that installs a fixture tool into `CapabilityRegistry`.

## What is stored

`mcp_servers` records the server key, display name, transport (`inprocess`), trust status (`unreviewed`, `reviewed`, `revoked`), owner, and last validation time. Connection secrets are not columns.

`mcp_tools` records the discovered name, capability id, description, input schema, schema hash, and registration state (`discovered`, `registered`, `disabled`, `revoked`). `available` stays false until registration. Risk and permission are null until a registrar sets them.

`capability_versions` stores each registered schema hash. `registration_events` records register, disable, and revoke.

## Fixture tools

The server key is `fixture`.

| Tool | Capability id | Apex risk | Permission | Idempotent |
| --- | --- | --- | --- | --- |
| `records_lookup` | `mcp.fixture.records_lookup` | assigned at registration, expected `low` | `records.lookup` | yes |
| `records_update` | `mcp.fixture.records_update` | assigned at registration, expected `high` | `records.update` | no |

Initial records are `alpha=open` and `beta=held`. Lookup reads that store. Update writes it and treats a repeated `operation_id` as the same update. If the response is lost after the write, the execution is `uncertain` and is not retried. Verification reads `read_record` rather than trusting the tool result.

Tool descriptions are stored as untrusted text. They do not set risk or permission.

## What is not implemented

- stdio, SSE, or remote MCP transports
- servers other than `fixture`
- automatic retry of a non-idempotent tool
- exactly-once delivery

A2A has no module.
