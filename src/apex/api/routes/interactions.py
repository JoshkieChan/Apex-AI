"""Interaction API. Identity comes from the bearer token, not the body."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from apex.api.deps import get_clock, get_session, require_principal
from apex.api.errors import ApiError
from apex.clock import Clock
from apex.config import Settings
from apex.errors import DomainError
from apex.interaction.contracts import InteractionCreate
from apex.interaction.service import InteractionService
from apex.persistence.models import Principal
from apex.providers.base import ModelProvider

router = APIRouter(prefix="/v1", tags=["interactions"])

_STATUS = {
    "not_found": 404,
    "media_unavailable": 409,
    "project_mismatch": 409,
    "idempotency_conflict": 409,
    "outcome_uncertain": 409,
    "unsupported_capability": 422,
    "invalid_media": 400,
    "unsupported_media": 400,
    "media_too_large": 400,
    "media_duration_exceeded": 400,
    "timeout": 504,
    "rate_limit": 429,
    "authentication": 502,
    "unavailable": 502,
    "invalid_output": 502,
    "unsupported": 422,
}


def _principal(request: Request, session: Session = Depends(get_session)) -> Principal:
    return require_principal(request, session)


def _service(
    request: Request,
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> InteractionService:
    settings: Settings = request.app.state.settings
    provider: ModelProvider = request.app.state.provider
    root: Path = request.app.state.media_root
    return InteractionService(session, clock, settings, provider, root)


@router.post("/media")
def upload_media(
    request: Request,
    file: UploadFile,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: InteractionService = Depends(_service),
) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    data = file.file.read(settings.media_max_bytes + 1)
    view = _call(session, lambda: service.ingest(principal, data).model_dump(mode="json"))
    return view


@router.get("/media/{media_id}")
def read_media_metadata(
    media_id: str,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: InteractionService = Depends(_service),
) -> dict[str, object]:
    def load() -> dict[str, object]:
        content, media_type = service.read_content(principal, media_id)
        return {"id": media_id, "media_type": media_type, "byte_size": len(content)}

    return _call(session, load)


@router.get("/media/{media_id}/content")
def read_media_content(
    media_id: str,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: InteractionService = Depends(_service),
) -> Response:
    try:
        content, media_type = service.read_content(principal, media_id)
    except DomainError as exc:
        session.rollback()
        raise _error(exc) from exc
    session.commit()
    return Response(content=content, media_type=media_type)


@router.post("/interactions")
def create_interaction(
    body: InteractionCreate,
    request: Request,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: InteractionService = Depends(_service),
) -> dict[str, object]:
    header_key = request.headers.get("Idempotency-Key")
    if header_key and body.idempotency_key and header_key != body.idempotency_key:
        raise ApiError(400, "idempotency_conflict", "The idempotency keys do not match.")
    if header_key is not None and (len(header_key) == 0 or len(header_key) > 128):
        raise ApiError(400, "invalid_request", "The idempotency key is not valid.")
    if header_key and not body.idempotency_key:
        body = body.model_copy(update={"idempotency_key": header_key})
    try:
        submitted = service.submit(principal, body)
    except DomainError as exc:
        session.rollback()
        raise _error(exc) from exc
    session.commit()
    for path in submitted.unlink:
        path.unlink(missing_ok=True)
    return submitted.view.model_dump(mode="json")


@router.get("/conversations/{conversation_id}")
def read_conversation(
    conversation_id: str,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: InteractionService = Depends(_service),
) -> dict[str, object]:
    return _call(
        session,
        lambda: service.conversation(principal, conversation_id).model_dump(mode="json"),
    )


def _call[T](session: Session, action: Callable[[], T]) -> T:
    try:
        result = action()
    except DomainError as exc:
        session.rollback()
        raise _error(exc) from exc
    session.commit()
    return result


def _error(exc: DomainError) -> ApiError:
    return ApiError(_STATUS.get(exc.code, 400), exc.code, exc.message)
