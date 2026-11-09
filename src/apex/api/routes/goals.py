"""Goal coordination routes. Handlers do not execute capabilities themselves."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from apex.api.deps import get_clock, get_session, require_principal
from apex.api.errors import ApiError
from apex.capabilities.registry import CapabilityRegistry
from apex.clock import Clock
from apex.config import Settings
from apex.coordination.service import CoordinationService
from apex.errors import DomainError
from apex.persistence.models import Principal

router = APIRouter(prefix="/v1/goals", tags=["goals"])


class GoalSubmit(BaseModel):
    """A coordinated objective. The original text is stored unchanged."""

    model_config = ConfigDict(extra="forbid")
    objective: str = Field(min_length=1, max_length=2000)
    project_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class AgentStatusBody(BaseModel):
    """A lifecycle change. It does not grant a permission."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["active", "disabled", "retired"]


def _principal(request: Request, session: Session = Depends(get_session)) -> Principal:
    return require_principal(request, session)


def _service(
    request: Request,
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> CoordinationService:
    settings: Settings = request.app.state.settings
    registry: CapabilityRegistry = request.app.state.registry
    return CoordinationService(session, settings, registry, clock)


def _call[T](session: Session, action: Callable[[], T]) -> T:
    try:
        result = action()
    except DomainError as exc:
        session.rollback()
        raise _error(exc) from exc
    session.commit()
    return result


@router.get("/agents")
def list_agents(
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: CoordinationService = Depends(_service),
) -> dict[str, object]:
    return _call(session, lambda: service.list_agents(principal))


@router.post("/agents/{agent_key}")
def set_agent_status(
    agent_key: str,
    body: AgentStatusBody,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: CoordinationService = Depends(_service),
) -> dict[str, object]:
    return _call(session, lambda: service.set_agent_status(principal, agent_key, body.status))


@router.post("")
def submit_goal(
    body: GoalSubmit,
    max_waves: int | None = None,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: CoordinationService = Depends(_service),
) -> dict[str, object]:
    return _call(
        session,
        lambda: service.submit(principal, body.objective, body.project_key, max_waves=max_waves),
    )


@router.get("/{goal_id}")
def read_goal(
    goal_id: str,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: CoordinationService = Depends(_service),
) -> dict[str, object]:
    return _call(session, lambda: service.view(goal_id, principal))


@router.post("/{goal_id}/run")
def run_goal(
    goal_id: str,
    max_waves: int | None = None,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: CoordinationService = Depends(_service),
) -> dict[str, object]:
    return _call(session, lambda: service.advance(goal_id, principal, max_waves=max_waves))


@router.post("/{goal_id}/replan")
def replan_goal(
    goal_id: str,
    max_waves: int | None = None,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: CoordinationService = Depends(_service),
) -> dict[str, object]:
    return _call(session, lambda: service.replan(goal_id, principal, max_waves=max_waves))


@router.post("/{goal_id}/cancel")
def cancel_goal(
    goal_id: str,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: CoordinationService = Depends(_service),
) -> dict[str, object]:
    return _call(session, lambda: service.cancel(goal_id, principal))


def _error(exc: DomainError) -> ApiError:
    status = {
        "permission_denied": 403,
        "role_forbidden": 403,
        "not_found": 404,
        "outcome_uncertain": 409,
        "conflict": 409,
    }.get(exc.code, 400)
    return ApiError(status, exc.code, exc.message)
