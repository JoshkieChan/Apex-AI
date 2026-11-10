"""Operator controls. They do not execute a capability."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from apex.api.deps import get_clock, get_session, require_principal
from apex.api.errors import ApiError
from apex.authority.engine import AuthorityEngine
from apex.clock import Clock
from apex.contracts.schemas import Decision
from apex.operations.controls import execution_suspended, set_execution_suspended
from apex.persistence.models import Principal

router = APIRouter(prefix="/v1", tags=["operations"])


class ExecutionHoldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suspended: bool


class ExecutionHoldView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suspended: bool


def _principal(request: Request, session: Session = Depends(get_session)) -> Principal:
    return require_principal(request, session)


def _require_operator(session: Session, principal: Principal) -> None:
    decision = AuthorityEngine(session).evaluate(principal.id, "operations.suspend")
    if decision != Decision.allow:
        raise ApiError(403, "permission_denied", "This principal cannot suspend execution.")


@router.get("/operations/execution", response_model=ExecutionHoldView)
def read_execution_hold(
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
) -> ExecutionHoldView:
    """Report the hold. This does not change a task."""
    _require_operator(session, principal)
    return ExecutionHoldView(suspended=execution_suspended(session))


@router.post("/operations/execution", response_model=ExecutionHoldView)
def write_execution_hold(
    body: ExecutionHoldRequest,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> ExecutionHoldView:
    """Stop or resume new capability execution. Uncertain rows are left unchanged."""
    _require_operator(session, principal)
    enabled = set_execution_suspended(
        session,
        enabled=body.suspended,
        actor_id=principal.id,
        now=clock.now(),
    )
    return ExecutionHoldView(suspended=enabled)
