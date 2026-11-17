"""Load accepted connections into a process registry. The database remains authoritative."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from apex.capabilities.registry import CapabilityRegistry
from apex.clock import ensure_aware
from apex.config import Settings
from apex.connectors.gates import ConnectionGate, set_gate
from apex.connectors.service import ConnectionService
from apex.mcp.registration import McpRegistrationService
from apex.persistence.models import (
    Connection,
    ConnectionOperation,
    McpServerRecord,
    McpToolRecord,
)


def hydrate_registry(session: Session, registry: CapabilityRegistry, settings: Settings) -> None:
    """Install accepted operations and replace process-local ceilings from current rows.

    A revoked, disabled, expired, or schema-changed operation is not installed.
    An operation that was installed earlier in this process is disabled.
    """
    connections = list(session.scalars(select(Connection)).all())
    by_server = {
        connection.mcp_server_id: connection
        for connection in connections
        if connection.mcp_server_id is not None
    }
    for connection in connections:
        gate = ConnectionGate(
            state=connection.state,
            scopes=frozenset(connection.scopes or []),
            project_key=connection.project_key,
            expires_at=connection.expires_at,
        )
        set_gate(connection.id, gate)
        if connection.mcp_server_id is not None:
            set_gate(connection.mcp_server_id, gate)
    mcp = McpRegistrationService(session, registry, settings)
    for tool in session.scalars(select(McpToolRecord)).all():
        server = session.get(McpServerRecord, tool.server_id)
        owned = None if server is None else by_server.get(server.id)
        if server is not None and _mcp_usable(tool, server, owned):
            mcp.install_registered(tool, server)
            continue
        registry.disable(tool.capability_id)
    operations = ConnectionService(session, registry, settings)
    for operation in session.scalars(select(ConnectionOperation)).all():
        owned = session.get(Connection, operation.connection_id)
        if owned is not None and _operation_usable(operation, owned):
            operations.install_registered(owned, operation)
            continue
        registry.disable(operation.capability_id)


def connection_is_current(connection: Connection, now: datetime | None = None) -> bool:
    """Return whether a persisted connection may accept a new dispatch."""
    moment = now or datetime.now(UTC)
    if connection.state != "active":
        return False
    expires = connection.expires_at
    if expires is not None and ensure_aware(expires) <= ensure_aware(moment):
        return False
    return True


def bind_execution_ceiling(session: Session, capability_id: str) -> None:
    """Refresh the process ceiling from the committed row before an adapter runs.

    The adapter does not open a database connection. A missing connection leaves
    a legacy MCP tool ungated. A revoked or disabled row replaces a stale gate.
    """
    tool = session.scalar(select(McpToolRecord).where(McpToolRecord.capability_id == capability_id))
    connection: Connection | None
    gate_key: str | None
    if tool is not None:
        gate_key = tool.server_id
        connection = session.scalar(
            select(Connection).where(Connection.mcp_server_id == tool.server_id)
        )
    else:
        operation = session.scalar(
            select(ConnectionOperation).where(ConnectionOperation.capability_id == capability_id)
        )
        if operation is None:
            return
        connection = session.get(Connection, operation.connection_id)
        gate_key = None if connection is None else connection.id
    if connection is None or gate_key is None:
        if gate_key is not None:
            set_gate(gate_key, None)
        return
    gate = ConnectionGate(
        state=connection.state,
        scopes=frozenset(connection.scopes or []),
        project_key=connection.project_key,
        expires_at=connection.expires_at,
    )
    set_gate(gate_key, gate)
    set_gate(connection.id, gate)
    if connection.kind != "device":
        return
    from apex.connectors.reference import mark_device_paired, read_device, remember_device
    from apex.persistence.models import DeviceRecord

    device = session.scalar(select(DeviceRecord).where(DeviceRecord.connection_id == connection.id))
    if device is None or read_device(device.device_key) is not None:
        return
    remember_device(device.device_key)
    if device.pairing_state == "paired":
        mark_device_paired(device.device_key)


def _mcp_usable(
    tool: McpToolRecord, server: McpServerRecord, connection: Connection | None
) -> bool:
    registered = (
        tool.registration_state == "registered"
        and tool.available
        and server.trust_status == "reviewed"
    )
    if connection is None:
        return registered
    return registered and connection_is_current(connection)


def _operation_usable(operation: ConnectionOperation, connection: Connection) -> bool:
    return (
        connection_is_current(connection)
        and operation.registration_state == "registered"
        and operation.available
    )
