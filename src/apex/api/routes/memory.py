"""Governed memory API. Routes do not read the database except through the store."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from apex.api.deps import get_clock, get_session, require_principal
from apex.api.errors import ApiError
from apex.clock import Clock
from apex.config import Settings
from apex.errors import DomainError
from apex.memory.contracts import (
    MemoryContextRequest,
    MemoryCreate,
    MemoryForget,
    MemorySearch,
    MemoryUpdate,
    MemoryVerify,
)
from apex.memory.service import MemoryService
from apex.persistence.models import Principal

router = APIRouter(prefix="/v1/memory", tags=["memory"])


def _principal(request: Request, session: Session = Depends(get_session)) -> Principal:
    return require_principal(request, session)


def _service(
    request: Request,
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> MemoryService:
    settings: Settings = request.app.state.settings
    return MemoryService(session, clock, settings)


def _call[T](session: Session, action: Callable[[], T]) -> T:
    try:
        result = action()
    except DomainError as exc:
        if exc.code == "not_found":
            session.commit()
        else:
            session.rollback()
        raise _error(exc) from exc
    session.commit()
    return result


@router.post("")
def create_memory(
    body: MemoryCreate,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: MemoryService = Depends(_service),
) -> dict[str, object]:
    return _call(session, lambda: service.store.create(principal, body).model_dump(mode="json"))


@router.post("/search")
def search_memory(
    body: MemorySearch,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: MemoryService = Depends(_service),
) -> dict[str, object]:
    def _search() -> dict[str, object]:
        rows = service.store.search(principal, body)
        return {"memories": [row.model_dump(mode="json") for row in rows]}

    return _call(session, _search)


@router.post("/context")
def assemble_context(
    body: MemoryContextRequest,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: MemoryService = Depends(_service),
) -> dict[str, object]:
    def _assemble() -> dict[str, object]:
        context = service.store.assemble(
            principal,
            query=body.query,
            task_id=body.task_id,
            project_key=body.project_key,
            budget=body.budget_characters,
            delegation_id=body.delegation_id,
        )
        return context.model_dump(mode="json")

    return _call(session, _assemble)


@router.post("/forget")
def forget_memory(
    body: MemoryForget,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: MemoryService = Depends(_service),
) -> dict[str, object]:
    return _call(session, lambda: {"deleted": service.store.forget(principal, body)})


@router.post("/purge")
def purge_memory(
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: MemoryService = Depends(_service),
) -> dict[str, object]:
    return _call(session, lambda: {"purged": service.store.purge_due(principal)})


@router.get("/{memory_id}")
def get_memory(
    memory_id: str,
    delegation_id: str | None = None,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: MemoryService = Depends(_service),
) -> dict[str, object]:
    return _call(
        session,
        lambda: service.store.get(principal, memory_id, delegation_id).model_dump(mode="json"),
    )


@router.get("/{memory_id}/history")
def memory_history(
    memory_id: str,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: MemoryService = Depends(_service),
) -> dict[str, object]:
    def _history() -> dict[str, object]:
        rows = service.store.history(principal, memory_id)
        return {"revisions": [row.model_dump(mode="json") for row in rows]}

    return _call(session, _history)


@router.patch("/{memory_id}")
def update_memory(
    memory_id: str,
    body: MemoryUpdate,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: MemoryService = Depends(_service),
) -> dict[str, object]:
    return _call(
        session,
        lambda: service.store.update(principal, memory_id, body).model_dump(mode="json"),
    )


@router.post("/{memory_id}/verify")
def verify_memory(
    memory_id: str,
    body: MemoryVerify,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: MemoryService = Depends(_service),
) -> dict[str, object]:
    return _call(
        session,
        lambda: service.store.verify(principal, memory_id, body).model_dump(mode="json"),
    )


@router.post("/{memory_id}/dispute")
def dispute_memory(
    memory_id: str,
    body: dict[str, int],
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: MemoryService = Depends(_service),
) -> dict[str, object]:
    expected = body.get("expected_version")
    if not isinstance(expected, int):
        raise ApiError(422, "invalid_request", "Request validation failed.")

    return _call(
        session,
        lambda: service.store.dispute(principal, memory_id, expected).model_dump(mode="json"),
    )


@router.post("/{memory_id}/supersede")
def supersede_memory(
    memory_id: str,
    body: MemoryCreate,
    expected_version: int,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: MemoryService = Depends(_service),
) -> dict[str, object]:
    return _call(
        session,
        lambda: service.store.supersede(principal, memory_id, body, expected_version).model_dump(
            mode="json"
        ),
    )


@router.delete("/{memory_id}")
def delete_memory(
    memory_id: str,
    delegation_id: str | None = None,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: MemoryService = Depends(_service),
) -> dict[str, object]:
    return _call(
        session,
        lambda: service.store.delete(principal, memory_id, delegation_id).model_dump(mode="json"),
    )


def _error(exc: DomainError) -> ApiError:
    status = {
        "permission_denied": 403,
        "scope_violation": 403,
        "approval_required": 403,
        "not_found": 404,
        "conflict": 409,
        "sensitive_content": 400,
        "invalid_memory": 400,
    }.get(exc.code, 400)
    return ApiError(status, exc.code, exc.message)
