"""Inspect and reconcile an uncertain execution. The route does not call the original tool."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from apex.api.deps import get_clock, get_session, require_principal
from apex.api.errors import ApiError
from apex.clock import Clock
from apex.errors import DomainError
from apex.persistence.models import Principal
from apex.reconciliation.service import ReconciliationService

router = APIRouter(prefix="/v1", tags=["reconciliation"])


def _principal(request: Request, session: Session = Depends(get_session)) -> Principal:
    return require_principal(request, session)


def _service(
    request: Request,
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> ReconciliationService:
    return ReconciliationService(session, request.app.state.settings, clock)


@router.get("/executions/{execution_id}/reconciliation")
def reconciliation_status(
    execution_id: str,
    principal: Principal = Depends(_principal),
    service: ReconciliationService = Depends(_service),
) -> dict[str, object]:
    try:
        return service.status(execution_id, principal)
    except DomainError as exc:
        raise _error(exc) from exc


@router.post("/executions/{execution_id}/reconcile")
def reconcile(
    execution_id: str,
    principal: Principal = Depends(_principal),
    service: ReconciliationService = Depends(_service),
) -> dict[str, object]:
    try:
        return service.reconcile(execution_id, principal)
    except DomainError as exc:
        raise _error(exc) from exc


def _error(exc: DomainError) -> ApiError:
    status = {
        "permission_denied": 403,
        "not_found": 404,
        "conflict": 409,
        "reconciliation_exhausted": 409,
        "not_reconcilable": 409,
    }.get(exc.code, 400)
    return ApiError(status, exc.code, exc.message)
