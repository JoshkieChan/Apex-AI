"""Liveness and readiness. A live process is not ready when it cannot do its work."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from apex import __version__
from apex.api.deps import authenticator_for, get_session
from apex.contracts.schemas import HealthResponse
from apex.errors import DomainError
from apex.persistence.models import Execution, Goal
from apex.persistence.schema import SCHEMA_REVISION, schema_compatible
from apex.secrets import build_secret_provider

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
@router.get("/v1/health", response_model=HealthResponse)
def health(request: Request, session: Session = Depends(get_session)) -> HealthResponse:
    """Report process and database availability. This does not prove production readiness."""
    database: Literal["ok", "unavailable"] = "ok"
    try:
        session.execute(text("SELECT 1"))
    except Exception:
        database = "unavailable"
    diagnostics = _diagnostics(request, session) if database == "ok" else {}
    return HealthResponse(
        status="ok",
        database=database,
        environment=request.app.state.settings.environment,
        auth_mode=_auth_mode(request),
        version=__version__,
        schema_revision=SCHEMA_REVISION,
        secret_provider=_provider_name(request),
        recoverable_goals=int(diagnostics.get("recoverable_goals", 0)),
        leased_goals=int(diagnostics.get("leased_goals", 0)),
        uncertain_operations=int(diagnostics.get("uncertain_operations", 0)),
        awaiting_approval=int(diagnostics.get("awaiting_approval", 0)),
    )


@router.get("/ready")
def ready(request: Request, session: Session = Depends(get_session)) -> JSONResponse:
    """Readiness. Liveness stays on /health and does not claim that authentication is configured."""
    database_ok = True
    try:
        session.execute(text("SELECT 1"))
    except Exception:
        database_ok = False
    authenticator = authenticator_for(request)
    secrets_ok = _secrets_ready(request)
    if (
        database_ok
        and authenticator.active()
        and schema_compatible(session, request.app.state.settings)
        and secrets_ok
    ):
        return JSONResponse(
            status_code=200,
            content={"status": "ready", "auth_mode": _auth_mode(request)},
        )
    return JSONResponse(
        status_code=503,
        content={"status": "not_ready", "auth_mode": _auth_mode(request)},
    )


def _diagnostics(request: Request, session: Session) -> dict[str, int]:
    now = request.app.state.clock.now()
    try:
        recoverable = session.scalar(
            select(func.count())
            .select_from(Goal)
            .where(Goal.status.in_(("planned", "running")))
            .where(or_(Goal.lease_expires_at.is_(None), Goal.lease_expires_at < now))
        )
        leased = session.scalar(
            select(func.count())
            .select_from(Goal)
            .where(Goal.lease_expires_at.is_not(None), Goal.lease_expires_at >= now)
        )
        uncertain = session.scalar(
            select(func.count()).select_from(Execution).where(Execution.status == "uncertain")
        )
        waiting = session.scalar(
            select(func.count()).select_from(Goal).where(Goal.status == "awaiting_approval")
        )
    except Exception:
        session.rollback()
        return {}
    return {
        "recoverable_goals": int(recoverable or 0),
        "leased_goals": int(leased or 0),
        "uncertain_operations": int(uncertain or 0),
        "awaiting_approval": int(waiting or 0),
    }


def _provider_name(request: Request) -> str:
    try:
        return build_secret_provider(request.app.state.settings).name
    except DomainError:
        return "unavailable"


def _secrets_ready(request: Request) -> bool:
    settings = request.app.state.settings
    try:
        provider = build_secret_provider(settings)
    except DomainError:
        return False
    if settings.require_external_secrets or settings.secret_provider != "env":  # noqa: S105
        return provider.ready()
    return True


def _auth_mode(request: Request) -> Literal["development", "bearer", "unconfigured"]:
    settings = request.app.state.settings
    authenticator = authenticator_for(request)
    if not authenticator.configured():
        return "unconfigured"
    if settings.environment == "production":
        return "bearer"
    if settings.dev_auth_enabled:
        return "development"
    return "unconfigured"
