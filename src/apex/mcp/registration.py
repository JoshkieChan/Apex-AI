"""Explicit MCP registration. Discovery never enables a tool."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from apex.capabilities.registry import CapabilityRegistry, CapabilitySpec
from apex.config import Settings
from apex.contracts.schemas import RiskLevel
from apex.errors import DomainError
from apex.mcp.fixture import invoke_fixture_tool
from apex.netpolicy import validate_remote_url
from apex.persistence.models import (
    CapabilityVersion,
    McpServerRecord,
    McpToolRecord,
    RegistrationEvent,
)
from apex.runtime_ids import canonical_json, new_id
from apex.secrets import validate_secret_reference

_PERMISSION = re.compile(r"^[a-z][a-z0-9_.]{1,64}$")
_CONTROL = {
    "authority.approve",
    "authority.grant",
    "capability.register",
    "operations.reconcile",
    "operations.suspend",
}


class LookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class LookupOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    found: bool
    name: str
    record: dict[str, Any] | None


class UpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    status: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    operation_id: str = Field(min_length=1, max_length=64)


class UpdateOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    applied: bool
    name: str
    status: str
    operation_id: str


def _digest(schema: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(schema).encode("utf-8")).hexdigest()


class McpRegistrationService:
    """Review servers and register tools under Apex-assigned risk and permissions."""

    def __init__(
        self,
        session: Session,
        registry: CapabilityRegistry,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._registry = registry
        self._settings = settings

    def add_server(
        self,
        *,
        owner_id: str,
        server_key: str,
        display_name: str,
        now: datetime,
        transport: str = "inprocess",
        endpoint: str | None = None,
        credential_ref: str | None = None,
    ) -> McpServerRecord:
        if server_key == "fixture":
            if transport != "inprocess" or endpoint or credential_ref:
                raise DomainError(
                    "unsupported_server",
                    "The fixture server does not accept a remote endpoint.",
                )
        elif transport != "http" or not endpoint:
            raise DomainError(
                "unsupported_server", "A remote MCP server requires an HTTP endpoint."
            )
        else:
            if self._settings is None:
                raise DomainError("endpoint_rejected", "Remote MCP is not configured.")
            validate_remote_url(
                endpoint,
                self._settings,
                allow_local_fixture=self._settings.mcp_allow_local_fixture,
            )
            if credential_ref:
                validate_secret_reference(credential_ref)
        existing = self._session.scalar(
            select(McpServerRecord).where(McpServerRecord.server_key == server_key)
        )
        if existing is not None:
            return existing
        record = McpServerRecord(
            id=new_id(),
            server_key=server_key,
            display_name=display_name,
            description="Registered MCP server. Credentials are stored only as references.",
            transport="inprocess" if server_key == "fixture" else "http",
            endpoint=None if server_key == "fixture" else endpoint,
            credential_ref=None if server_key == "fixture" else credential_ref,
            tls_required=bool(endpoint and endpoint.startswith("https://")),
            trust_status="unreviewed",
            owner_principal_id=owner_id,
            last_validated_at=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def discover(
        self, server_key: str, tools: list[dict[str, Any]], now: datetime
    ) -> list[McpToolRecord]:
        server = self._require_server(server_key)
        saved: list[McpToolRecord] = []
        for tool in tools:
            name = str(tool.get("name", ""))
            schema = tool.get("input_schema")
            if not isinstance(schema, dict) or schema.get("type") not in {None, "object"}:
                raise DomainError("invalid_schema", "A discovered tool schema is not an object.")
            capability_id = f"mcp.{server.server_key}.{name}"
            row = self._session.scalar(
                select(McpToolRecord).where(
                    McpToolRecord.server_id == server.id,
                    McpToolRecord.tool_name == name,
                )
            )
            digest = _digest(schema)
            if (
                row is not None
                and row.registration_state == "registered"
                and row.schema_hash != digest
            ):
                row.input_schema = schema
                row.schema_hash = digest
                row.version = str(int(row.version) + 1)
                row.registration_state = "schema_changed"
                row.available = False
                row.updated_at = now
                self._registry.disable(row.capability_id)
                saved.append(row)
                continue
            if row is None:
                row = McpToolRecord(
                    id=new_id(),
                    server_id=server.id,
                    tool_name=name,
                    capability_id=capability_id,
                    description=str(tool.get("description", ""))[:2000],
                    input_schema=schema,
                    risk=None,
                    permission_key=None,
                    verification_kind=None,
                    registration_state="discovered",
                    schema_hash=_digest(schema),
                    version="0",
                    available=False,
                    registered_by_principal_id=None,
                    created_at=now,
                    updated_at=now,
                )
                self._session.add(row)
            elif row.registration_state == "discovered":
                row.description = str(tool.get("description", ""))[:2000]
                row.input_schema = schema
                row.schema_hash = _digest(schema)
                row.updated_at = now
            saved.append(row)
        server.trust_status = "reviewed"
        server.last_validated_at = now
        server.updated_at = now
        self._session.flush()
        return saved

    def register(
        self,
        *,
        actor_id: str,
        tool_id: str,
        risk: str,
        permission_key: str,
        verification_kind: str,
        now: datetime,
    ) -> McpToolRecord:
        if risk not in {RiskLevel.low.value, RiskLevel.medium.value, RiskLevel.high.value}:
            raise DomainError("invalid_registration", "Risk must be assigned by Apex.")
        if not _PERMISSION.fullmatch(permission_key) or permission_key in _CONTROL:
            raise DomainError("invalid_registration", "The permission key is not acceptable.")
        tool = self._session.get(McpToolRecord, tool_id)
        if tool is None or tool.registration_state == "revoked":
            raise DomainError("tool_not_found", "The discovered tool is not available to register.")
        server = self._session.get(McpServerRecord, tool.server_id)
        if server is None or server.trust_status != "reviewed":
            raise DomainError("server_unreviewed", "The MCP server has not been reviewed.")
        version = "1" if tool.version == "0" else str(int(tool.version) + 1)
        self._install(tool, server, risk, permission_key, verification_kind, version)
        tool.risk = risk
        tool.permission_key = permission_key
        tool.verification_kind = verification_kind
        tool.registration_state = "registered"
        tool.available = True
        tool.version = version
        tool.registered_by_principal_id = actor_id
        tool.updated_at = now
        self._session.add(
            CapabilityVersion(
                id=new_id(),
                capability_id=tool.capability_id,
                version=version,
                schema_hash=tool.schema_hash,
                input_schema=dict(tool.input_schema),
                created_at=now,
            )
        )
        self._event(tool.id, actor_id, "registered", {"version": version, "risk": risk}, now)
        self._session.flush()
        return tool

    def disable(self, *, actor_id: str, tool_id: str, now: datetime) -> McpToolRecord:
        tool = self._session.get(McpToolRecord, tool_id)
        if tool is None or tool.registration_state != "registered":
            raise DomainError("tool_not_found", "The capability is not registered.")
        tool.registration_state = "disabled"
        tool.available = False
        tool.updated_at = now
        self._registry.disable(tool.capability_id)
        self._event(tool.id, actor_id, "disabled", {}, now)
        self._session.flush()
        return tool

    def revoke(self, *, actor_id: str, tool_id: str, now: datetime) -> McpToolRecord:
        tool = self._session.get(McpToolRecord, tool_id)
        if tool is None:
            raise DomainError("tool_not_found", "The capability is not registered.")
        tool.registration_state = "revoked"
        tool.available = False
        tool.updated_at = now
        self._registry.disable(tool.capability_id)
        server = self._session.get(McpServerRecord, tool.server_id)
        if server is not None:
            server.trust_status = "revoked"
            server.updated_at = now
        self._event(tool.id, actor_id, "revoked", {}, now)
        self._session.flush()
        return tool

    def _install(
        self,
        tool: McpToolRecord,
        server: McpServerRecord,
        risk: str,
        permission_key: str,
        verification_kind: str,
        version: str,
    ) -> None:
        if tool.tool_name == "records_lookup":
            model_in: type[BaseModel] = LookupInput
            model_out: type[BaseModel] = LookupOutput
            idempotent = True
            attempts = 2
        elif tool.tool_name == "records_update":
            model_in = UpdateInput
            model_out = UpdateOutput
            idempotent = False
            attempts = 1
        else:
            raise DomainError("unsupported_tool", "This tool has no Apex adapter.")
        try:
            model_in.model_validate({})
        except ValidationError:
            pass
        spec = CapabilitySpec(
            id=tool.capability_id,
            name=tool.tool_name,
            description="Registered MCP capability. Server metadata is not a permission.",
            permission_key=permission_key,
            risk=RiskLevel(risk),
            input_model=model_in,
            output_model=model_out,
            adapter=_adapter(server, tool.tool_name, self._settings, permission_key),
            verification_kind=verification_kind,
            timeout_seconds=2.0,
            max_attempts=attempts,
            version=version,
            idempotent=idempotent,
            schema_hash=tool.schema_hash,
            resource_fields=("name",),
            supports_status_reconciliation=(
                server.transport == "inprocess" and tool.tool_name == "records_update"
            ),
            supports_independent_readback=(
                server.transport == "inprocess" and tool.tool_name == "records_update"
            ),
            supports_cancellation=False,
            verification_method=(
                "independent_readback"
                if server.transport == "inprocess" and tool.tool_name == "records_update"
                else "none"
            ),
        )
        self._registry.replace(spec)

    def _require_server(self, server_key: str) -> McpServerRecord:
        server = self._session.scalar(
            select(McpServerRecord).where(McpServerRecord.server_key == server_key)
        )
        if server is None:
            raise DomainError("server_not_found", "The MCP server is not registered.")
        return server

    def _event(
        self, tool_id: str, actor_id: str, action: str, detail: dict[str, Any], now: datetime
    ) -> None:
        self._session.add(
            RegistrationEvent(
                id=new_id(),
                mcp_tool_id=tool_id,
                actor_principal_id=actor_id,
                action=action,
                detail=detail,
                created_at=now,
            )
        )


def _adapter(
    server: McpServerRecord,
    tool_name: str,
    settings: Settings | None,
    permission_key: str,
) -> Callable[[Mapping[str, object], float], dict[str, object]]:
    transport = server.transport
    endpoint = server.endpoint or ""
    credential_ref = server.credential_ref
    server_id = server.id

    def run(payload: Mapping[str, object], timeout_seconds: float) -> dict[str, object]:
        from datetime import UTC, datetime

        from apex.connectors.gates import enforce

        enforce(server_id, permission_key, None, datetime.now(UTC))
        arguments = {str(key): value for key, value in payload.items()}
        if transport == "inprocess":
            return cast(
                "dict[str, object]",
                invoke_fixture_tool(tool_name, arguments, timeout_seconds),
            )
        if settings is None:
            raise DomainError("capability_unavailable", "The MCP server is not configured.")
        from apex.mcp.remote import invoke_remote_tool

        return cast(
            "dict[str, object]",
            invoke_remote_tool(
                endpoint=endpoint,
                credential_ref=credential_ref,
                tool_name=tool_name,
                arguments=arguments,
                timeout_seconds=timeout_seconds,
                settings=settings,
            ),
        )

    return run
