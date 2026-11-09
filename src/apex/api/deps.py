"""Request dependencies. Identity comes from a validated bearer token."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from apex.api.errors import ApiError
from apex.authority.engine import AuthorityEngine
from apex.clock import Clock
from apex.contracts.schemas import Decision
from apex.identity.resolution import principal_for_subject
from apex.identity.tokens import AuthFailure, TokenAuthenticator
from apex.persistence.models import Principal
from apex.runtime.factory import build_orchestrator
from apex.runtime.orchestrator import Orchestrator

APPROVE_PERMISSION = "authority.approve"
REGISTER_PERMISSION = "capability.register"


def get_session(request: Request) -> Iterator[Session]:
    """Yield a session for one request. The runtime commits its own units of work."""
    session = request.app.state.session_factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_clock(request: Request) -> Clock:
    return request.app.state.clock


def get_orchestrator(request: Request, session: Session) -> Orchestrator:
    return build_orchestrator(
        session=session,
        settings=request.app.state.settings,
        registry=request.app.state.registry,
        provider=request.app.state.provider,
        clock=request.app.state.clock,
    )


def authenticator_for(request: Request) -> TokenAuthenticator:
    current = getattr(request.app.state, "authenticator", None)
    if isinstance(current, TokenAuthenticator):
        return current
    created = TokenAuthenticator(request.app.state.settings)
    request.app.state.authenticator = created
    return created


def require_principal(request: Request, session: Session) -> Principal:
    """Resolve the principal from a validated token. Headers cannot select another principal."""
    if request.query_params.get("principal_id"):
        raise ApiError(400, "impersonation_rejected", "A caller cannot select a principal.")
    settings = request.app.state.settings
    header_principal = request.headers.get("X-Apex-Principal")
    authorization = request.headers.get("Authorization", "")
    authenticator = authenticator_for(request)
    if settings.environment == "production" and header_principal and not authorization:
        raise ApiError(
            401,
            "development_auth_rejected",
            "Development identity is not accepted in production.",
        )
    if not authenticator.active():
        raise ApiError(
            401,
            "authentication_not_configured",
            "This server has no authentication mechanism configured.",
        )
    if not authorization.startswith("Bearer ") or not authorization[7:].strip():
        raise ApiError(401, "missing_credentials", "A bearer token is required.")
    token = authorization[7:].strip()
    try:
        claims = authenticator.validate(token)
    except AuthFailure as exc:
        raise ApiError(401, exc.code, exc.message) from exc
    principal = principal_for_subject(session, claims.issuer, claims.subject)
    if principal is None:
        raise ApiError(401, "unknown_principal", "The token subject is not recognized.")
    return principal


def can_review_approvals(session: Session, principal_id: str) -> bool:
    """Return whether the principal may read and decide approval requests."""
    decision = AuthorityEngine(session).evaluate(principal_id, APPROVE_PERMISSION)
    return decision == Decision.allow


def can_register_capabilities(session: Session, principal_id: str) -> bool:
    decision = AuthorityEngine(session).evaluate(principal_id, REGISTER_PERMISSION)
    return decision == Decision.allow
