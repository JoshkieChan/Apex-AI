"""Task submission, retrieval, cancellation, and approval decisions."""

from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from apex.api.deps import can_review_approvals, get_orchestrator, get_session, require_principal
from apex.api.errors import ApiError
from apex.contracts.schemas import (
    ApprovalDecisionRequest,
    ApprovalView,
    TaskCreateRequest,
    TaskStatusView,
    TaskView,
)
from apex.errors import DomainError
from apex.persistence.models import Principal
from apex.persistence.repository import get_approval, get_task
from apex.runtime.orchestrator import Orchestrator
from apex.runtime.views import status_view
from apex.runtime_ids import new_id

router = APIRouter(prefix="/v1", tags=["tasks"])


def _principal(
    request: Request,
    session: Session = Depends(get_session),
) -> Principal:
    return require_principal(request, session)


def _orchestrator(
    request: Request,
    session: Session = Depends(get_session),
) -> Orchestrator:
    return get_orchestrator(request, session)


def _raise_domain(exc: DomainError) -> NoReturn:
    status = {
        "task_not_found": 404,
        "approval_not_found": 404,
        "approver_not_authorized": 403,
        "separation_of_duties": 403,
        "not_cancellable": 409,
        "approval_already_decided": 409,
        "approval_expired": 409,
        "approval_mismatch": 409,
        "approval_replayed": 409,
        "schema_changed": 409,
        "conflict": 409,
        "delegation_forbidden": 403,
        "delegation_exists": 409,
    }.get(exc.code, 400)
    raise ApiError(status, exc.code, exc.message) from exc


@router.post("/tasks", response_model=TaskView)
def create_task(
    body: TaskCreateRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(_principal),
    orchestrator: Orchestrator = Depends(_orchestrator),
) -> TaskView:
    """Submit a request. A repeated idempotency key returns the original task."""
    header_key = request.headers.get("Idempotency-Key")
    if header_key and body.idempotency_key and header_key != body.idempotency_key:
        raise ApiError(400, "idempotency_conflict", "The idempotency keys do not match.")
    if header_key is not None and (len(header_key) == 0 or len(header_key) > 128):
        raise ApiError(400, "invalid_request", "The idempotency key is not valid.")
    request_id = request.headers.get("X-Request-ID") or new_id()
    response.headers["X-Request-ID"] = request_id
    try:
        view, created = orchestrator.submit(
            principal_id=principal.id,
            request_text=body.request,
            request_id=request_id,
            idempotency_key=header_key or body.idempotency_key,
            project_key=body.project_key,
            delegation_id=body.delegation_id,
        )
    except DomainError as exc:
        _raise_domain(exc)
    response.status_code = 201 if created else 200
    response.headers["Idempotency-Replayed"] = "false" if created else "true"
    return view


@router.get("/tasks/{task_id}", response_model=TaskView)
def get_one_task(
    task_id: str,
    response: Response,
    request: Request,
    principal: Principal = Depends(_principal),
    orchestrator: Orchestrator = Depends(_orchestrator),
) -> TaskView:
    """Return one task belonging to the calling principal."""
    response.headers["X-Request-ID"] = request.headers.get("X-Request-ID") or new_id()
    try:
        return orchestrator.view_for_owner(task_id, principal.id)
    except DomainError as exc:
        _raise_domain(exc)


@router.get("/tasks/{task_id}/status", response_model=TaskStatusView)
def get_task_status(
    task_id: str,
    response: Response,
    request: Request,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
) -> TaskStatusView:
    """Return the lifecycle status for a task owned by the calling principal."""
    response.headers["X-Request-ID"] = request.headers.get("X-Request-ID") or new_id()
    task = get_task(session, task_id)
    if task is None or task.principal_id != principal.id:
        raise ApiError(404, "task_not_found", "Task not found.")
    return status_view(task)


@router.post("/tasks/{task_id}/cancel", response_model=TaskView)
def cancel_task(
    task_id: str,
    response: Response,
    request: Request,
    principal: Principal = Depends(_principal),
    orchestrator: Orchestrator = Depends(_orchestrator),
) -> TaskView:
    """Cancel a task before execution starts."""
    response.headers["X-Request-ID"] = request.headers.get("X-Request-ID") or new_id()
    try:
        return orchestrator.cancel(principal_id=principal.id, task_id=task_id)
    except DomainError as exc:
        _raise_domain(exc)


@router.get("/approvals/{approval_id}", response_model=ApprovalView)
def get_one_approval(
    approval_id: str,
    response: Response,
    request: Request,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
) -> ApprovalView:
    """Return an approval to its requester or to a principal who can decide approvals."""
    response.headers["X-Request-ID"] = request.headers.get("X-Request-ID") or new_id()
    approval = get_approval(session, approval_id)
    task = get_task(session, approval.task_id) if approval is not None else None
    if approval is None or task is None:
        raise ApiError(404, "approval_not_found", "Approval not found.")
    if task.principal_id != principal.id and not can_review_approvals(session, principal.id):
        raise ApiError(404, "approval_not_found", "Approval not found.")
    from apex.runtime.views import task_view

    for item in task_view(session, task).approvals:
        if item.id == approval.id:
            return item
    raise ApiError(404, "approval_not_found", "Approval not found.")


@router.post("/approvals/{approval_id}/decision", response_model=TaskView)
def decide_approval(
    approval_id: str,
    body: ApprovalDecisionRequest,
    response: Response,
    request: Request,
    principal: Principal = Depends(_principal),
    orchestrator: Orchestrator = Depends(_orchestrator),
) -> TaskView:
    """Approve or reject an operation, then resume the task when that is legal."""
    response.headers["X-Request-ID"] = request.headers.get("X-Request-ID") or new_id()
    try:
        return orchestrator.decide_approval(
            approver_id=principal.id,
            approval_id=approval_id,
            decision=body.decision,
            reason=body.reason,
        )
    except DomainError as exc:
        _raise_domain(exc)
