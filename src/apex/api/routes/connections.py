"""Connection routes. Handlers do not execute capabilities."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from apex.api.deps import can_register_capabilities, get_clock, get_session, require_principal
from apex.api.errors import ApiError
from apex.capabilities.registry import CapabilityRegistry
from apex.clock import Clock
from apex.config import Settings
from apex.connectors.contracts import ConnectionCreate, OperationAccept, PairRequest, ScopeGrant
from apex.connectors.service import ConnectionService
from apex.errors import DomainError
from apex.persistence.models import DeviceRecord, Principal

router = APIRouter(prefix="/v1", tags=["connections"])


def _principal(request: Request, session: Session = Depends(get_session)) -> Principal:
    return require_principal(request, session)


def _service(request: Request, session: Session = Depends(get_session)) -> ConnectionService:
    settings: Settings = request.app.state.settings
    registry: CapabilityRegistry = request.app.state.registry
    return ConnectionService(session, registry, settings)


def _registrar(session: Session, principal: Principal) -> None:
    if not can_register_capabilities(session, principal.id):
        raise ApiError(
            403,
            "registrar_not_authorized",
            "This principal cannot register a connection.",
        )


@router.post("/connections")
def register_connection(
    body: ConnectionCreate,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
    service: ConnectionService = Depends(_service),
) -> dict[str, object]:
    """Record a connection. A device response includes the pairing code once."""
    _registrar(session, principal)
    try:
        view, pairing_code = service.register(actor_id=principal.id, body=body, now=clock.now())
    except DomainError as exc:
        raise _error(exc) from exc
    session.commit()
    payload = view.model_dump(mode="json")
    if pairing_code is not None:
        device = session.scalar(select(DeviceRecord).where(DeviceRecord.connection_id == view.id))
        payload["pairing_code"] = pairing_code
        payload["device_id"] = None if device is None else device.id
    return payload


@router.get("/connections/{connection_id}")
def read_connection(
    connection_id: str,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: ConnectionService = Depends(_service),
) -> dict[str, object]:
    try:
        view = service.get_connection(actor_id=principal.id, connection_id=connection_id)
    except DomainError as exc:
        raise _error(exc) from exc
    return view.model_dump(mode="json")


@router.post("/connections/{connection_id}/authorize")
def authorize_connection(
    connection_id: str,
    body: ScopeGrant,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
    service: ConnectionService = Depends(_service),
) -> dict[str, object]:
    _registrar(session, principal)
    return _commit(
        session,
        lambda: service.authorize(
            actor_id=principal.id,
            connection_id=connection_id,
            scopes=body.scopes,
            expires_at=body.expires_at,
            now=clock.now(),
        ).model_dump(mode="json"),
    )


@router.post("/connections/{connection_id}/discover")
def discover_connection(
    connection_id: str,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
    service: ConnectionService = Depends(_service),
) -> dict[str, object]:
    _registrar(session, principal)
    return _commit(
        session,
        lambda: {
            "operations": [
                item.model_dump(mode="json")
                for item in service.discover(
                    actor_id=principal.id,
                    connection_id=connection_id,
                    now=clock.now(),
                )
            ]
        },
    )


@router.post("/connections/{connection_id}/operations/{operation_id}/accept")
def accept_operation(
    connection_id: str,
    operation_id: str,
    body: OperationAccept,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
    service: ConnectionService = Depends(_service),
) -> dict[str, object]:
    _registrar(session, principal)
    return _commit(
        session,
        lambda: service.accept(
            actor_id=principal.id,
            connection_id=connection_id,
            operation_id=operation_id,
            body=body,
            now=clock.now(),
        ).model_dump(mode="json"),
    )


@router.post("/connections/{connection_id}/disable")
def disable_connection(
    connection_id: str,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
    service: ConnectionService = Depends(_service),
) -> dict[str, object]:
    _registrar(session, principal)
    return _commit(
        session,
        lambda: service.disable(
            actor_id=principal.id, connection_id=connection_id, now=clock.now()
        ).model_dump(mode="json"),
    )


@router.post("/connections/{connection_id}/revoke")
def revoke_connection(
    connection_id: str,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
    service: ConnectionService = Depends(_service),
) -> dict[str, object]:
    _registrar(session, principal)
    return _commit(
        session,
        lambda: service.revoke(
            actor_id=principal.id, connection_id=connection_id, now=clock.now()
        ).model_dump(mode="json"),
    )


@router.post("/connections/{connection_id}/disconnect")
def disconnect_connection(
    connection_id: str,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
    service: ConnectionService = Depends(_service),
) -> dict[str, object]:
    _registrar(session, principal)
    return _commit(
        session,
        lambda: service.disconnect(
            actor_id=principal.id, connection_id=connection_id, now=clock.now()
        ).model_dump(mode="json"),
    )


@router.post("/connections/{connection_id}/health")
def connection_health(
    connection_id: str,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
    service: ConnectionService = Depends(_service),
) -> dict[str, object]:
    _registrar(session, principal)
    return _commit(
        session,
        lambda: service.health(
            actor_id=principal.id, connection_id=connection_id, now=clock.now()
        ).model_dump(mode="json"),
    )


@router.post("/devices/{device_id}/pair")
def pair_device(
    device_id: str,
    body: PairRequest,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
    service: ConnectionService = Depends(_service),
) -> dict[str, object]:
    _registrar(session, principal)
    return _commit(
        session,
        lambda: service.pair(
            actor_id=principal.id,
            device_id=device_id,
            pairing_code=body.pairing_code,
            now=clock.now(),
        ).model_dump(mode="json"),
    )


@router.get("/devices/{device_id}")
def read_device(
    device_id: str,
    principal: Principal = Depends(_principal),
    session: Session = Depends(get_session),
    service: ConnectionService = Depends(_service),
) -> dict[str, object]:
    try:
        view = service.get_device(actor_id=principal.id, device_id=device_id)
    except DomainError as exc:
        raise _error(exc) from exc
    return view.model_dump(mode="json")


def _commit[T](session: Session, action: Callable[[], T]) -> T:
    try:
        result = action()
    except DomainError as exc:
        session.rollback()
        raise _error(exc) from exc
    session.commit()
    return result


def _error(exc: DomainError) -> ApiError:
    status = {
        "not_found": 404,
        "registrar_not_authorized": 403,
        "connection_unavailable": 409,
        "credential_expired": 409,
        "insufficient_scope": 403,
        "project_mismatch": 409,
        "conflict": 409,
        "pairing_rejected": 403,
        "endpoint_rejected": 400,
        "invalid_secret_reference": 400,
        "invalid_registration": 400,
    }.get(exc.code, 400)
    return ApiError(status, exc.code, exc.message)
