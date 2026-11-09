"""Registration, delegation, and identity routes. They do not execute capabilities."""

from __future__ import annotations

import hmac
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from apex.api.deps import (
    can_register_capabilities,
    get_clock,
    get_session,
    require_principal,
)
from apex.api.errors import ApiError
from apex.api.locality import local_peer, proxy_indicated
from apex.authority.delegation import DelegationService
from apex.authority.engine import AuthorityEngine
from apex.capabilities.registry import CapabilityRegistry
from apex.clock import Clock
from apex.errors import DomainError
from apex.identity.tokens import AuthFailure, issue_dev_token
from apex.mcp.fixture import discover_fixture_tools
from apex.mcp.registration import McpRegistrationService
from apex.persistence.models import McpToolRecord, Principal
from apex.persistence.repository import get_grant, get_principal_by_key

router = APIRouter(prefix="/v1", tags=["administration"])


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DevTokenRequest(ModelConfig):
    subject: str = Field(min_length=1, max_length=128)


class ServerRequest(ModelConfig):
    server_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    display_name: str = Field(min_length=1, max_length=256)
    transport: str = "inprocess"
    endpoint: str | None = None
    credential_ref: str | None = None


class RegisterToolRequest(ModelConfig):
    risk: str = Field(pattern=r"^(low|medium|high)$")
    permission_key: str = Field(min_length=1, max_length=128)
    verification_kind: str = Field(min_length=1, max_length=128)


class DelegationRequest(ModelConfig):
    delegate_subject: str = Field(min_length=1, max_length=128)
    permission_key: str = Field(min_length=1, max_length=128)
    resource_scope: str = "*"
    expires_at: datetime


class RevokeGrantRequest(ModelConfig):
    subject: str = Field(min_length=1, max_length=128)
    permission_key: str = Field(min_length=1, max_length=128)


def _principal(request: Request, session: Session = Depends(get_session)) -> Principal:
    return require_principal(request, session)


def _require_registrar(session: Session, principal: Principal) -> None:
    if not can_register_capabilities(session, principal.id):
        raise ApiError(
            403, "registrar_not_authorized", "This principal cannot register capabilities."
        )


@router.get("/principal")
def current_principal(principal: Principal = Depends(_principal)) -> dict[str, str]:
    """Return the authenticated principal. The caller cannot substitute another id."""
    return {
        "id": principal.id,
        "subject": principal.external_key,
        "display_name": principal.display_name,
    }


@router.post("/dev/tokens")
def issue_token(
    body: DevTokenRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, str]:
    """Issue a development token to a local caller that presents the bootstrap value."""
    settings = request.app.state.settings
    if (
        settings.environment == "production"
        or not settings.dev_auth_enabled
        or not settings.dev_token_issuance
        or not local_peer(request, settings)
        or proxy_indicated(request)
    ):
        raise ApiError(404, "not_found", "Not found.")
    supplied = request.headers.get("x-apex-dev-bootstrap", "")
    expected = settings.dev_token_bootstrap
    if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise ApiError(404, "not_found", "Not found.")
    if get_principal_by_key(session, body.subject) is None:
        raise ApiError(401, "unknown_principal", "The development subject is not recognized.")
    try:
        token = issue_dev_token(settings, body.subject)
    except AuthFailure as exc:
        raise ApiError(401, exc.code, exc.message) from exc
    return {"token_type": "Bearer", "access_token": token}


@router.post("/mcp/servers")
def add_server(
    body: ServerRequest,
    request: Request,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> dict[str, str]:
    _require_registrar(session, principal)
    try:
        record = McpRegistrationService(
            session, _registry(request), request.app.state.settings
        ).add_server(
            owner_id=principal.id,
            server_key=body.server_key,
            display_name=body.display_name,
            now=clock.now(),
            transport=body.transport,
            endpoint=body.endpoint,
            credential_ref=body.credential_ref,
        )
    except DomainError as exc:
        raise ApiError(400, exc.code, exc.message) from exc
    session.commit()
    return {"id": record.id, "server_key": record.server_key, "trust_status": record.trust_status}


@router.post("/mcp/servers/{server_key}/discover")
def discover(
    server_key: str,
    request: Request,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> dict[str, object]:
    _require_registrar(session, principal)
    service = McpRegistrationService(session, _registry(request), request.app.state.settings)
    try:
        server = service._require_server(server_key)
        if server.transport == "inprocess":
            tools = discover_fixture_tools()
        elif server.transport == "http":
            from apex.mcp.remote import list_remote_tools

            tools = list_remote_tools(server, request.app.state.settings)
        else:
            raise DomainError("unsupported_server", "The MCP transport is not supported.")
        saved = service.discover(server_key, tools, clock.now())
    except DomainError as exc:
        status = 404 if exc.code == "server_not_found" else 400
        raise ApiError(status, exc.code, exc.message) from exc
    session.commit()
    return {
        "tools": [
            {
                "id": tool.id,
                "tool_name": tool.tool_name,
                "capability_id": tool.capability_id,
                "registration_state": tool.registration_state,
                "available": tool.available,
            }
            for tool in saved
        ]
    }


@router.get("/mcp/tools")
def list_tools(
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    _require_registrar(session, principal)
    rows = session.scalars(select(McpToolRecord).order_by(McpToolRecord.tool_name))
    return {
        "tools": [
            {
                "id": row.id,
                "tool_name": row.tool_name,
                "capability_id": row.capability_id,
                "registration_state": row.registration_state,
                "available": row.available,
                "risk": row.risk,
                "version": row.version,
            }
            for row in rows
        ]
    }


@router.post("/mcp/tools/{tool_id}/register")
def register_tool(
    tool_id: str,
    body: RegisterToolRequest,
    request: Request,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> dict[str, object]:
    _require_registrar(session, principal)
    try:
        tool = McpRegistrationService(
            session, _registry(request), request.app.state.settings
        ).register(
            actor_id=principal.id,
            tool_id=tool_id,
            risk=body.risk,
            permission_key=body.permission_key,
            verification_kind=body.verification_kind,
            now=clock.now(),
        )
    except DomainError as exc:
        raise ApiError(400, exc.code, exc.message) from exc
    session.commit()
    return {
        "id": tool.id,
        "capability_id": tool.capability_id,
        "available": tool.available,
        "version": tool.version,
        "risk": tool.risk,
    }


@router.post("/mcp/tools/{tool_id}/disable")
def disable_tool(
    tool_id: str,
    request: Request,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> dict[str, str]:
    _require_registrar(session, principal)
    try:
        tool = McpRegistrationService(
            session, _registry(request), request.app.state.settings
        ).disable(actor_id=principal.id, tool_id=tool_id, now=clock.now())
    except DomainError as exc:
        raise ApiError(400, exc.code, exc.message) from exc
    session.commit()
    return {"id": tool.id, "registration_state": tool.registration_state}


@router.post("/mcp/tools/{tool_id}/revoke")
def revoke_tool(
    tool_id: str,
    request: Request,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> dict[str, str]:
    _require_registrar(session, principal)
    try:
        tool = McpRegistrationService(
            session, _registry(request), request.app.state.settings
        ).revoke(actor_id=principal.id, tool_id=tool_id, now=clock.now())
    except DomainError as exc:
        raise ApiError(400, exc.code, exc.message) from exc
    session.commit()
    return {"id": tool.id, "registration_state": tool.registration_state}


@router.post("/delegations")
def create_delegation(
    body: DelegationRequest,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> dict[str, str]:
    delegate = get_principal_by_key(session, body.delegate_subject)
    if delegate is None:
        raise ApiError(404, "unknown_principal", "The delegate is not recognized.")
    try:
        record = DelegationService(session).create(
            delegator=principal,
            delegate=delegate,
            permission_key=body.permission_key,
            resource_scope=body.resource_scope,
            expires_at=body.expires_at,
            now=clock.now(),
        )
    except DomainError as exc:
        status = 409 if exc.code == "delegation_exists" else 403
        raise ApiError(status, exc.code, exc.message) from exc
    session.commit()
    return {"id": record.id, "permission_key": record.permission_key}


@router.post("/grants/revoke")
def revoke_grant(
    body: RevokeGrantRequest,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> dict[str, str]:
    if AuthorityEngine(session).evaluate(principal.id, "authority.grant").value != "allow":
        raise ApiError(403, "grant_not_authorized", "This principal cannot revoke grants.")
    subject = get_principal_by_key(session, body.subject)
    if subject is None:
        raise ApiError(404, "unknown_principal", "The subject is not recognized.")
    grant = get_grant(session, subject.id, body.permission_key)
    if grant is None:
        raise ApiError(404, "grant_not_found", "The grant does not exist.")
    grant.effect = "deny"
    grant.revoked_at = clock.now()
    session.commit()
    return {"subject": body.subject, "permission_key": body.permission_key, "effect": "deny"}


def _registry(request: Request) -> CapabilityRegistry:
    registry = request.app.state.registry
    if not isinstance(registry, CapabilityRegistry):
        raise ApiError(500, "internal_error", "The capability registry is unavailable.")
    return registry
