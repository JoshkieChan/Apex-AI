"""Connection lifecycle. This module does not execute a capability."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from secrets import token_hex
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from apex.capabilities.registry import CapabilityRegistry, CapabilitySpec
from apex.config import Settings
from apex.connectors.catalog import (
    API_ENDPOINT,
    DEVICE_ENDPOINT,
    api_catalog,
    device_catalog,
    input_model,
    output_model,
    reference_adapter,
)
from apex.connectors.contracts import (
    CatalogOperation,
    ConnectionCreate,
    ConnectionView,
    DeviceView,
    OperationAccept,
    OperationView,
)
from apex.connectors.gates import ConnectionGate, set_gate
from apex.connectors.reference import (
    mark_device_paired,
    mark_device_unavailable,
    remember_device,
)
from apex.contracts.schemas import RiskLevel
from apex.errors import DomainError
from apex.logging_config import get_logger
from apex.mcp.registration import McpRegistrationService
from apex.persistence.models import (
    CapabilityVersion,
    Connection,
    ConnectionOperation,
    DeviceRecord,
    McpServerRecord,
    McpToolRecord,
)
from apex.runtime_ids import canonical_json, new_id
from apex.secrets import validate_secret_reference

logger = get_logger("connectors")

_MCP_SCOPES = frozenset({"records.lookup", "records.update"})


class ConnectionService:
    """Record, authorize, and revoke connections. Adapters run later, in the engine."""

    def __init__(
        self,
        session: Session,
        registry: CapabilityRegistry,
        settings: Settings,
    ) -> None:
        self._session = session
        self._registry = registry
        self._settings = settings

    def register(
        self, *, actor_id: str, body: ConnectionCreate, now: datetime
    ) -> tuple[ConnectionView, str | None]:
        """Record a connection. The optional string is a one-time device pairing code."""
        existing = self._session.scalar(
            select(Connection).where(Connection.connection_key == body.connection_key)
        )
        if existing is not None:
            self._same_registration(existing, actor_id, body)
            return self._view(existing), None
        endpoint, secret_ref, server = self._prepare(actor_id, body, now)
        connection = Connection(
            id=new_id(),
            connection_key=body.connection_key,
            kind=body.kind,
            display_name=body.display_name,
            owner_principal_id=actor_id,
            project_key=body.project_key,
            external_identity=body.external_identity,
            endpoint=endpoint,
            auth_method="secret_ref" if secret_ref else "none",
            secret_ref=secret_ref,
            scopes=[],
            trust="untrusted",
            state="pending_authorization",
            mcp_server_id=None if server is None else server.id,
            capability_fingerprint="",
            expires_at=None,
            last_health_at=None,
            revoked_at=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(connection)
        self._session.flush()
        pairing_code: str | None = None
        if body.kind == "device":
            pairing_code = token_hex(16)
            self._session.add(
                DeviceRecord(
                    id=new_id(),
                    device_key=body.connection_key,
                    connection_id=connection.id,
                    device_type="reference",
                    owner_principal_id=actor_id,
                    pairing_state="unpaired",
                    pairing_code_hash=_hash_code(pairing_code),
                    protocol_version="1",
                    health="unknown",
                    last_seen_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            remember_device(body.connection_key)
        self._refresh_gate(connection)
        self._log(connection, "registered")
        self._session.flush()
        return self._view(connection), pairing_code

    def authorize(
        self,
        *,
        actor_id: str,
        connection_id: str,
        scopes: list[str],
        expires_at: datetime | None,
        now: datetime,
    ) -> ConnectionView:
        """Store connection scopes. This does not grant the principal a permission."""
        connection = self._owned(actor_id, connection_id)
        self._reject_closed(connection)
        allowed = self._scope_ceiling(connection)
        if any(scope not in allowed for scope in scopes):
            raise DomainError("invalid_registration", "A requested scope is not allowed.")
        connection.scopes = list(dict.fromkeys(scopes))
        connection.expires_at = expires_at
        connection.updated_at = now
        if connection.kind == "device" and not self._device_paired(connection):
            connection.state = "pending_authorization"
        else:
            connection.state = "active"
        self._refresh_gate(connection)
        self._log(connection, "authorized")
        self._session.flush()
        return self._view(connection)

    def discover(
        self,
        *,
        actor_id: str,
        connection_id: str,
        now: datetime,
        schemas: dict[str, dict[str, Any]] | None = None,
    ) -> list[OperationView]:
        """List operations. A schema change disables a previously accepted operation."""
        connection = self._owned(actor_id, connection_id)
        self._reject_closed(connection)
        if connection.kind == "mcp":
            return self._discover_mcp(connection, now)
        catalog = api_catalog() if connection.kind == "api" else device_catalog()
        saved: list[OperationView] = []
        fingerprints: list[tuple[str, str]] = []
        for operation in catalog:
            schema = dict(operation.input_schema)
            if schemas is not None and operation.operation_key in schemas:
                schema = schemas[operation.operation_key]
            digest = _digest(schema)
            fingerprints.append((operation.operation_key, digest))
            saved.append(self._upsert_operation(connection, operation, schema, digest, now))
        connection.capability_fingerprint = _digest({key: value for key, value in fingerprints})
        connection.trust = "reviewed"
        connection.updated_at = now
        self._log(connection, "discovered")
        self._session.flush()
        return saved

    def accept(
        self,
        *,
        actor_id: str,
        connection_id: str,
        operation_id: str,
        body: OperationAccept,
        now: datetime,
    ) -> OperationView:
        """Install one reviewed operation. The caller cannot lower its Apex risk."""
        connection = self._owned(actor_id, connection_id)
        if connection.state != "active":
            raise DomainError(
                "connection_unavailable",
                "The connection is not active.",
            )
        if connection.kind == "mcp":
            return self._accept_mcp(actor_id, connection, operation_id, body, now)
        operation = self._session.get(ConnectionOperation, operation_id)
        if (
            operation is None
            or operation.connection_id != connection.id
            or operation.registration_state in {"revoked", "disabled"}
        ):
            raise DomainError("operation_not_found", "The operation is not available to accept.")
        catalog = self._catalog_item(connection.kind, operation.operation_key)
        if body.permission_key != catalog.permission_key or body.risk != catalog.risk:
            raise DomainError(
                "invalid_registration",
                "Risk and permission are assigned by Apex.",
            )
        if catalog.permission_key not in set(connection.scopes):
            raise DomainError(
                "insufficient_scope",
                "The connection scope does not include this operation.",
            )
        version = "1" if operation.version == "0" else str(int(operation.version) + 1)
        self._install(connection, operation, catalog, version)
        operation.risk = catalog.risk
        operation.permission_key = catalog.permission_key
        operation.registration_state = "registered"
        operation.available = True
        operation.version = version
        operation.updated_at = now
        self._session.add(
            CapabilityVersion(
                id=new_id(),
                capability_id=operation.capability_id,
                version=version,
                schema_hash=operation.schema_hash,
                input_schema=dict(operation.input_schema),
                created_at=now,
            )
        )
        self._log(connection, "accepted")
        self._session.flush()
        return _operation_view(operation)

    def disable(self, *, actor_id: str, connection_id: str, now: datetime) -> ConnectionView:
        """Stop new work. Rows and audit history stay."""
        connection = self._owned(actor_id, connection_id)
        self._reject_closed(connection)
        connection.state = "disabled"
        connection.updated_at = now
        self._quiesce(connection, "disabled", now)
        self._refresh_gate(connection)
        self._log(connection, "disabled")
        self._session.flush()
        return self._view(connection)

    def revoke(self, *, actor_id: str, connection_id: str, now: datetime) -> ConnectionView:
        """Revoke the connection. Uncertain executions are not replayed or deleted."""
        connection = self._owned(actor_id, connection_id)
        connection.state = "revoked"
        connection.revoked_at = now
        connection.updated_at = now
        self._quiesce(connection, "revoked", now)
        device = self._device(connection)
        if device is not None:
            device.pairing_state = "revoked"
            device.health = "unavailable"
            device.updated_at = now
            mark_device_unavailable(device.device_key)
        self._refresh_gate(connection)
        self._log(connection, "revoked")
        self._session.flush()
        return self._view(connection)

    def disconnect(self, *, actor_id: str, connection_id: str, now: datetime) -> ConnectionView:
        """Mark the connection degraded. This is not revocation."""
        connection = self._owned(actor_id, connection_id)
        self._reject_closed(connection)
        connection.state = "degraded"
        connection.updated_at = now
        device = self._device(connection)
        if device is not None:
            device.health = "unavailable"
            device.updated_at = now
            mark_device_unavailable(device.device_key)
        self._refresh_gate(connection)
        self._log(connection, "disconnected")
        self._session.flush()
        return self._view(connection)

    def health(self, *, actor_id: str, connection_id: str, now: datetime) -> ConnectionView:
        """Refresh health for an active reference connection. Revoked stays revoked."""
        connection = self._owned(actor_id, connection_id)
        if connection.state in {"revoked", "disabled", "failed"}:
            return self._view(connection)
        device = self._device(connection)
        if device is not None and device.health == "unavailable":
            connection.state = "degraded"
        elif connection.scopes and (
            connection.kind != "device" or (device is not None and device.pairing_state == "paired")
        ):
            connection.state = "active"
            if device is not None:
                device.health = "ok"
                device.last_seen_at = now
                device.updated_at = now
        connection.last_health_at = now
        connection.updated_at = now
        self._refresh_gate(connection)
        self._log(connection, "health")
        self._session.flush()
        return self._view(connection)

    def pair(
        self, *, actor_id: str, device_id: str, pairing_code: str, now: datetime
    ) -> DeviceView:
        """Pair a device the actor owns. A wrong code does not activate it."""
        device = self._session.get(DeviceRecord, device_id)
        if device is None or device.owner_principal_id != actor_id:
            raise DomainError("not_found", "The device is not available.")
        if device.pairing_state == "revoked":
            raise DomainError("connection_unavailable", "The device pairing was revoked.")
        if not hmac.compare_digest(_hash_code(pairing_code), device.pairing_code_hash):
            raise DomainError("pairing_rejected", "The pairing code is not valid.")
        connection = self._session.get(Connection, device.connection_id)
        if connection is None or connection.state in {"revoked", "disabled"}:
            raise DomainError("connection_unavailable", "The connection cannot be paired.")
        device.pairing_state = "paired"
        device.health = "ok"
        device.last_seen_at = now
        device.updated_at = now
        mark_device_paired(device.device_key)
        if connection.scopes:
            connection.state = "active"
        connection.updated_at = now
        self._refresh_gate(connection)
        self._log(connection, "paired")
        self._session.flush()
        return _device_view(device)

    def get_connection(self, *, actor_id: str, connection_id: str) -> ConnectionView:
        """Return one connection owned by the actor."""
        return self._view(self._owned(actor_id, connection_id))

    def get_device(self, *, actor_id: str, device_id: str) -> DeviceView:
        """Return one device owned by the actor."""
        device = self._session.get(DeviceRecord, device_id)
        if device is None or device.owner_principal_id != actor_id:
            raise DomainError("not_found", "The device is not available.")
        return _device_view(device)

    def _prepare(
        self, actor_id: str, body: ConnectionCreate, now: datetime
    ) -> tuple[str | None, str | None, McpServerRecord | None]:
        if body.kind == "api":
            if body.endpoint != API_ENDPOINT or body.secret_ref or body.server_key:
                raise DomainError("endpoint_rejected", "The API endpoint is not allowed.")
            return API_ENDPOINT, None, None
        if body.kind == "device":
            if body.endpoint != DEVICE_ENDPOINT or body.secret_ref or body.server_key:
                raise DomainError("endpoint_rejected", "The device endpoint is not allowed.")
            return DEVICE_ENDPOINT, None, None
        if not body.server_key:
            raise DomainError("invalid_registration", "An MCP connection needs a server key.")
        secret = body.secret_ref
        if secret:
            validate_secret_reference(secret)
        server = McpRegistrationService(self._session, self._registry, self._settings).add_server(
            owner_id=actor_id,
            server_key=body.server_key,
            display_name=body.display_name,
            now=now,
            transport="inprocess" if body.server_key == "fixture" else "http",
            endpoint=None if body.server_key == "fixture" else body.endpoint,
            credential_ref=None if body.server_key == "fixture" else secret,
        )
        return server.endpoint, server.credential_ref, server

    def _discover_mcp(self, connection: Connection, now: datetime) -> list[OperationView]:
        server = self._server(connection)
        if server.transport == "inprocess":
            from apex.mcp.fixture import discover_fixture_tools

            tools = discover_fixture_tools()
        else:
            from apex.mcp.remote import list_remote_tools

            tools = list_remote_tools(server, self._settings)
        saved = McpRegistrationService(self._session, self._registry, self._settings).discover(
            server.server_key, tools, now
        )
        connection.trust = "reviewed"
        connection.capability_fingerprint = _digest(
            {tool.tool_name: tool.schema_hash for tool in saved}
        )
        connection.updated_at = now
        self._log(connection, "discovered")
        return [
            OperationView(
                id=tool.id,
                operation_key=tool.tool_name,
                capability_id=tool.capability_id,
                registration_state=tool.registration_state,
                available=tool.available,
                schema_hash=tool.schema_hash,
                side_effect=(
                    "non_idempotent_write" if tool.tool_name == "records_update" else "read"
                ),
            )
            for tool in saved
        ]

    def _accept_mcp(
        self,
        actor_id: str,
        connection: Connection,
        operation_id: str,
        body: OperationAccept,
        now: datetime,
    ) -> OperationView:
        tool = self._session.get(McpToolRecord, operation_id)
        server = self._server(connection)
        if tool is None or tool.server_id != server.id:
            raise DomainError("operation_not_found", "The operation is not available to accept.")
        in_scope = body.permission_key in _MCP_SCOPES and body.permission_key in set(
            connection.scopes
        )
        if not in_scope:
            raise DomainError(
                "insufficient_scope",
                "The connection scope does not include this operation.",
            )
        registered = McpRegistrationService(self._session, self._registry, self._settings).register(
            actor_id=actor_id,
            tool_id=tool.id,
            risk=body.risk,
            permission_key=body.permission_key,
            verification_kind=body.permission_key,
            now=now,
        )
        self._log(connection, "accepted")
        return OperationView(
            id=registered.id,
            operation_key=registered.tool_name,
            capability_id=registered.capability_id,
            registration_state=registered.registration_state,
            available=registered.available,
            schema_hash=registered.schema_hash,
            side_effect=(
                "non_idempotent_write" if registered.tool_name == "records_update" else "read"
            ),
        )

    def _upsert_operation(
        self,
        connection: Connection,
        operation: CatalogOperation,
        schema: dict[str, Any],
        digest: str,
        now: datetime,
    ) -> OperationView:
        capability_id = f"{connection.kind}.{connection.connection_key}.{operation.operation_key}"
        row = self._session.scalar(
            select(ConnectionOperation).where(
                ConnectionOperation.connection_id == connection.id,
                ConnectionOperation.operation_key == operation.operation_key,
            )
        )
        if row is not None and row.registration_state == "registered" and row.schema_hash != digest:
            row.input_schema = schema
            row.schema_hash = digest
            row.version = str(int(row.version) + 1)
            row.registration_state = "schema_changed"
            row.available = False
            row.updated_at = now
            self._registry.disable(row.capability_id)
            return _operation_view(row)
        if row is None:
            row = ConnectionOperation(
                id=new_id(),
                connection_id=connection.id,
                operation_key=operation.operation_key,
                capability_id=capability_id,
                description=operation.description,
                input_schema=schema,
                output_schema=dict(operation.output_schema),
                schema_hash=digest,
                version="0",
                risk=None,
                permission_key=None,
                side_effect=operation.side_effect,
                registration_state="discovered",
                available=False,
                created_at=now,
                updated_at=now,
            )
            self._session.add(row)
            self._session.flush()
        elif row.registration_state == "discovered":
            row.input_schema = schema
            row.schema_hash = digest
            row.updated_at = now
        return _operation_view(row)

    def _install(
        self,
        connection: Connection,
        operation: ConnectionOperation,
        catalog: CatalogOperation,
        version: str,
    ) -> None:
        device = self._device(connection)
        spec = CapabilitySpec(
            id=operation.capability_id,
            name=operation.operation_key,
            description=catalog.description,
            permission_key=catalog.permission_key,
            risk=RiskLevel(catalog.risk),
            input_model=input_model(connection.kind, operation.operation_key),
            output_model=output_model(connection.kind, operation.operation_key),
            adapter=reference_adapter(
                gate_key=connection.id,
                kind=connection.kind,
                operation_key=operation.operation_key,
                permission=catalog.permission_key,
                device_key=None if device is None else device.device_key,
            ),
            verification_kind=catalog.verification_kind,
            timeout_seconds=2.0,
            max_attempts=1 if not catalog.idempotent else 2,
            version=version,
            idempotent=catalog.idempotent,
            schema_hash=operation.schema_hash,
            resource_fields=("device_key",) if connection.kind == "device" else ("item",),
            supports_status_reconciliation=operation.operation_key == "note",
            supports_independent_readback=True,
            supports_cancellation=False,
            verification_method="independent_readback",
        )
        self._registry.replace(spec)

    def _quiesce(self, connection: Connection, state: str, now: datetime) -> None:
        if connection.kind == "mcp":
            server = self._server(connection)
            tools = self._session.scalars(
                select(McpToolRecord).where(McpToolRecord.server_id == server.id)
            ).all()
            service = McpRegistrationService(self._session, self._registry, self._settings)
            for tool in tools:
                if state == "revoked":
                    service.revoke(
                        actor_id=connection.owner_principal_id,
                        tool_id=tool.id,
                        now=now,
                    )
                elif tool.registration_state == "registered":
                    service.disable(
                        actor_id=connection.owner_principal_id,
                        tool_id=tool.id,
                        now=now,
                    )
            return
        rows = self._session.scalars(
            select(ConnectionOperation).where(ConnectionOperation.connection_id == connection.id)
        ).all()
        for row in rows:
            if row.registration_state == "revoked":
                continue
            row.registration_state = state
            row.available = False
            row.updated_at = now
            self._registry.disable(row.capability_id)

    def _refresh_gate(self, connection: Connection) -> None:
        gate = ConnectionGate(
            state=connection.state,
            scopes=frozenset(connection.scopes or []),
            project_key=connection.project_key,
            expires_at=connection.expires_at,
        )
        set_gate(connection.id, gate)
        if connection.mcp_server_id:
            set_gate(connection.mcp_server_id, gate)

    def _owned(self, actor_id: str, connection_id: str) -> Connection:
        connection = self._session.get(Connection, connection_id)
        if connection is None or connection.owner_principal_id != actor_id:
            raise DomainError("not_found", "The connection is not available.")
        return connection

    def _server(self, connection: Connection) -> McpServerRecord:
        server = (
            None
            if connection.mcp_server_id is None
            else self._session.get(McpServerRecord, connection.mcp_server_id)
        )
        if server is None:
            raise DomainError("not_found", "The MCP server is not available.")
        return server

    def _device(self, connection: Connection) -> DeviceRecord | None:
        if connection.kind != "device":
            return None
        return self._session.scalar(
            select(DeviceRecord).where(DeviceRecord.connection_id == connection.id)
        )

    def _device_paired(self, connection: Connection) -> bool:
        device = self._device(connection)
        return device is not None and device.pairing_state == "paired"

    def _scope_ceiling(self, connection: Connection) -> frozenset[str]:
        if connection.kind == "mcp":
            return _MCP_SCOPES
        catalog = api_catalog() if connection.kind == "api" else device_catalog()
        return frozenset(item.permission_key for item in catalog)

    def _catalog_item(self, kind: str, operation_key: str) -> CatalogOperation:
        catalog = api_catalog() if kind == "api" else device_catalog()
        for item in catalog:
            if item.operation_key == operation_key:
                return item
        raise DomainError("unsupported_operation", "This operation is not in the Apex catalog.")

    def _reject_closed(self, connection: Connection) -> None:
        if connection.state in {"revoked", "disabled"}:
            raise DomainError("connection_unavailable", "The connection cannot accept work.")

    def _same_registration(
        self, existing: Connection, actor_id: str, body: ConnectionCreate
    ) -> None:
        if (
            existing.owner_principal_id != actor_id
            or existing.kind != body.kind
            or existing.project_key != body.project_key
        ):
            raise DomainError("conflict", "That connection key is already registered.")

    def _view(self, connection: Connection) -> ConnectionView:
        return ConnectionView(
            id=connection.id,
            connection_key=connection.connection_key,
            kind=connection.kind,
            display_name=connection.display_name,
            owner_principal_id=connection.owner_principal_id,
            project_key=connection.project_key,
            external_identity=connection.external_identity,
            endpoint=connection.endpoint,
            auth_method=connection.auth_method,
            secret_configured=bool(connection.secret_ref),
            scopes=list(connection.scopes or []),
            trust=connection.trust,
            state=connection.state,
            capability_fingerprint=connection.capability_fingerprint,
            expires_at=connection.expires_at,
            last_health_at=connection.last_health_at,
            revoked_at=connection.revoked_at,
        )

    def _log(self, connection: Connection, outcome: str) -> None:
        logger.info(
            "connection %s state=%s",
            outcome,
            connection.state,
            extra={
                "connection_id": connection.id,
                "connection_state": connection.state,
                "outcome": outcome,
            },
        )


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _operation_view(operation: ConnectionOperation) -> OperationView:
    return OperationView(
        id=operation.id,
        operation_key=operation.operation_key,
        capability_id=operation.capability_id,
        registration_state=operation.registration_state,
        available=operation.available,
        schema_hash=operation.schema_hash,
        side_effect=operation.side_effect,
    )


def _device_view(device: DeviceRecord) -> DeviceView:
    return DeviceView(
        id=device.id,
        device_key=device.device_key,
        connection_id=device.connection_id,
        device_type=device.device_type,
        owner_principal_id=device.owner_principal_id,
        pairing_state=device.pairing_state,
        protocol_version=device.protocol_version,
        health=device.health,
        last_seen_at=device.last_seen_at,
    )
