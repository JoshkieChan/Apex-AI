"""FastAPI application factory."""

from __future__ import annotations

import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.engine import Engine
from starlette.responses import Response

from apex import __version__
from apex.api.errors import ApiError
from apex.api.routes.admin import router as admin_router
from apex.api.routes.connections import router as connections_router
from apex.api.routes.goals import router as goals_router
from apex.api.routes.health import router as health_router
from apex.api.routes.interactions import router as interactions_router
from apex.api.routes.memory import router as memory_router
from apex.api.routes.operations import router as operations_router
from apex.api.routes.reconciliation import router as reconciliation_router
from apex.api.routes.tasks import router as tasks_router
from apex.capabilities.builtins import build_default_registry
from apex.clock import SystemClock
from apex.config import Settings, assert_runtime_is_allowed, load_settings
from apex.connectors.checks import install_connector_checks
from apex.identity.tokens import TokenAuthenticator
from apex.logging_config import configure_logging, get_logger
from apex.mcp.checks import install_mcp_checks
from apex.observability import setup_tracing
from apex.persistence.seed import seed_development_data
from apex.persistence.session import create_db_engine, create_session_factory
from apex.providers.factory import build_provider
from apex.runtime_ids import new_id


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    """Build the API. Tables are created by Alembic, not by this factory."""
    resolved = settings or load_settings()
    assert_runtime_is_allowed(resolved)
    configure_logging(resolved)
    setup_tracing()
    database = engine or create_db_engine(resolved.database_url)
    factory = create_session_factory(database)
    if resolved.seed_dev_principals:
        with factory() as session:
            seed_development_data(session, issuer=resolved.auth_issuer)
            session.commit()
    install_mcp_checks()
    install_connector_checks()
    app = FastAPI(title="Apex AI", version=__version__)
    app.state.settings = resolved
    app.state.engine = database
    app.state.session_factory = factory
    app.state.registry = build_default_registry()
    app.state.provider = build_provider(resolved)
    app.state.clock = SystemClock()
    app.state.authenticator = TokenAuthenticator(resolved)
    app.state.media_root = _media_root(resolved)
    origins = [item.strip() for item in resolved.cors_origins.split(",") if item.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        )
    app.include_router(health_router)
    app.include_router(tasks_router)
    app.include_router(admin_router)
    app.include_router(reconciliation_router)
    app.include_router(memory_router)
    app.include_router(goals_router)
    app.include_router(interactions_router)
    app.include_router(connections_router)
    app.include_router(operations_router)

    @app.middleware("http")
    async def add_security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.code, exc.message, _request_id(request)),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "loc": list(error.get("loc", [])),
                "msg": error.get("msg", ""),
                "type": error.get("type", ""),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_error_body(
                "invalid_request",
                "Request validation failed.",
                _request_id(request),
                details,
            ),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        get_logger("api").exception("unhandled API error request_id=%s", _request_id(request))
        del exc
        return JSONResponse(
            status_code=500,
            content=_error_body(
                "internal_error",
                "The request could not be completed.",
                _request_id(request),
            ),
        )

    return app


def _media_root(settings: Settings) -> Path:
    if settings.media_dir:
        root = Path(settings.media_dir)
        root.mkdir(parents=True, exist_ok=True)
        return root
    return Path(tempfile.mkdtemp(prefix="apex-media-"))


def _request_id(request: Request) -> str:
    return request.headers.get("X-Request-ID") or new_id()


def _error_body(
    code: str,
    message: str,
    request_id: str,
    details: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {"error": code, "message": message, "request_id": request_id}
    if details is not None:
        body["details"] = details
    return body
