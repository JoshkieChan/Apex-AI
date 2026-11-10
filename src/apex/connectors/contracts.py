"""Provider-independent connection contracts. Vendor clients do not appear here."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ConnectorKind = Literal["mcp", "api", "device"]
ConnectionState = Literal[
    "pending_configuration",
    "pending_authorization",
    "active",
    "degraded",
    "disabled",
    "revoked",
    "failed",
]
SideEffect = Literal["read", "idempotent_write", "non_idempotent_write"]


class ConnectionCreate(BaseModel):
    """A registrar's request to record a connection. It does not activate tools."""

    model_config = ConfigDict(extra="forbid")

    connection_key: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    kind: ConnectorKind
    display_name: str = Field(min_length=1, max_length=256)
    project_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    endpoint: str | None = None
    secret_ref: str | None = None
    server_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    external_identity: str = Field(default="", max_length=256)


class ScopeGrant(BaseModel):
    """Scopes the owner allows on this connection. They are not a principal grant."""

    model_config = ConfigDict(extra="forbid")

    scopes: list[str] = Field(min_length=1, max_length=8)
    expires_at: datetime | None = None


class OperationAccept(BaseModel):
    """Apex assigns the risk and permission. The connector cannot choose them."""

    model_config = ConfigDict(extra="forbid")

    risk: str = Field(pattern=r"^(low|medium|high)$")
    permission_key: str = Field(min_length=1, max_length=128)


class PairRequest(BaseModel):
    """The one-time pairing code returned at registration."""

    model_config = ConfigDict(extra="forbid")

    pairing_code: str = Field(min_length=16, max_length=128)


class ConnectionView(BaseModel):
    """Connection metadata. Secret values are not fields on this view."""

    model_config = ConfigDict(extra="forbid")

    id: str
    connection_key: str
    kind: str
    display_name: str
    owner_principal_id: str
    project_key: str | None
    external_identity: str
    endpoint: str | None
    auth_method: str
    secret_configured: bool
    scopes: list[str]
    trust: str
    state: str
    capability_fingerprint: str
    expires_at: datetime | None
    last_health_at: datetime | None
    revoked_at: datetime | None


class OperationView(BaseModel):
    """A discovered operation. Available stays false until Apex accepts it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    operation_key: str
    capability_id: str
    registration_state: str
    available: bool
    schema_hash: str
    side_effect: str


class DeviceView(BaseModel):
    """Device identity. It is not a principal and it is not a grant."""

    model_config = ConfigDict(extra="forbid")

    id: str
    device_key: str
    connection_id: str
    device_type: str
    owner_principal_id: str
    pairing_state: str
    protocol_version: str
    health: str
    last_seen_at: datetime | None


class CatalogOperation(BaseModel):
    """One Apex-defined operation. External metadata cannot add another."""

    model_config = ConfigDict(extra="forbid")

    operation_key: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    side_effect: SideEffect
    permission_key: str
    risk: str
    idempotent: bool
    verification_kind: str
