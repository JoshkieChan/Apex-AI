# Connectors and devices

A connection is Apex's record of an external system. The external system does not register itself, choose its risk, or grant a permission.

## Kinds

| Kind | Endpoint | What runs |
|---|---|---|
| `mcp` | An existing MCP server, or the in-process `fixture` server | The existing MCP registration and transport |
| `api` | `apex-reference://inventory` only | Lookup and one note operation |
| `device` | `apex-device://reference` only | Read and set the label of the paired reference device |

Any other URL is rejected. The API and device kinds do not open a socket. Private addresses, userinfo, and raw secret strings are rejected for MCP registration through the existing network policy and secret-reference check.

## Lifecycle

`pending_authorization` is the state after registration. `active` requires scopes and, for a device, a successful pair. `degraded` means disconnected. `disabled` and `revoked` stop new work and leave the rows. There is no transition that deletes audit records.

The pairing code is returned once on device registration. Only its hash is stored. A wrong code does not pair the device.

## Authority

The registrar needs `capability.register`. The caller who later runs an operation needs an Apex grant for that permission. The connection's scopes are a second ceiling checked in the adapter before the fixture changes. High-risk notes still require a different principal's approval. A role ceiling still applies to goal assignment: the researcher role cannot configure the device.

Schema drift marks the operation `schema_changed` and disables the capability. An old approval does not authorize the new schema. The engine already compares schema hash and version.

## Data

The reference inventory and device live in process memory. They are not governed memory and they are not sent to a model provider. A note is external state. Verification reads that state back. A lost response after the note is stored stays `uncertain`.

## Devices later

A future local device, sensor, or operator-controlled computer should be another connection kind with its own allowlisted operations, pairing, and gate. It should not scan the network, and it should not give a model a socket, a shell, or a filesystem.
