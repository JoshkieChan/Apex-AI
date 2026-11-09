"""Memory authorization. A stored sentence is not a grant."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from apex.authority.engine import AuthorityEngine
from apex.authority.scope import scope_allows
from apex.clock import ensure_aware
from apex.contracts.schemas import Decision
from apex.errors import DomainError
from apex.persistence.models import Delegation, MemoryRecord, PermissionGrant, Principal, Task
from apex.persistence.repository import get_grant


def require_memory_permission(
    session: Session,
    actor: Principal,
    permission: str,
    scope_id: str,
    *,
    now: datetime,
    delegation_id: str | None,
) -> None:
    """Allow the operation only when the current grant covers this scope."""
    decision = AuthorityEngine(session).evaluate(
        actor.id,
        permission,
        now=now,
        delegation_id=delegation_id,
    )
    if decision == Decision.deny:
        raise DomainError("permission_denied", "The principal cannot use that memory operation.")
    if decision != Decision.allow:
        raise DomainError(
            "approval_required",
            "The memory operation requires approval and was not executed.",
        )
    scope = _resource_scope(session, actor.id, permission, delegation_id)
    if not scope_allows(scope, scope_id):
        raise DomainError("scope_violation", "The memory is outside the granted scope.")


def can_manage(session: Session, actor: Principal, now: datetime) -> bool:
    decision = AuthorityEngine(session).evaluate(actor.id, "memory.manage", now=now)
    return decision == Decision.allow


def assert_scope_owner(
    session: Session,
    actor: Principal,
    scope_type: str,
    scope_id: str,
    *,
    now: datetime,
) -> None:
    """Reject a request that names a scope the actor does not own."""
    if scope_type == "personal" and scope_id != actor.id and not can_manage(session, actor, now):
        raise DomainError("permission_denied", "The principal cannot use that memory scope.")
    if scope_type == "task":
        task = session.get(Task, scope_id)
        if task is None or (task.principal_id != actor.id and not can_manage(session, actor, now)):
            raise DomainError("not_found", "The memory scope does not exist.")
    if scope_type == "system" and not can_manage(session, actor, now):
        raise DomainError("permission_denied", "System memory requires memory.manage.")
    if scope_type == "project" and not _project_visible(session, actor, scope_id, now):
        raise DomainError("permission_denied", "The principal cannot use that memory scope.")


def readable(
    session: Session,
    actor: Principal,
    record: MemoryRecord,
    *,
    now: datetime,
    delegation_id: str | None,
) -> None:
    """Hide unauthorized memory as not found so the caller cannot probe other owners."""
    try:
        require_memory_permission(
            session,
            actor,
            "memory.read",
            record.scope_id,
            now=now,
            delegation_id=delegation_id,
        )
        if record.scope_type == "personal" and record.owner_principal_id != actor.id:
            if not can_manage(session, actor, now):
                raise DomainError("permission_denied", "The memory is not visible.")
        elif record.scope_type == "task":
            assert_scope_owner(session, actor, "task", record.scope_id, now=now)
    except DomainError as exc:
        if exc.code == "permission_denied":
            raise DomainError("not_found", "The memory does not exist.") from exc
        raise


def _project_visible(session: Session, actor: Principal, scope_id: str, now: datetime) -> bool:
    if can_manage(session, actor, now):
        return True
    grant = _usable(session, actor.id, "memory.read", now)
    if grant is None or grant.effect == Decision.deny.value:
        return False
    return scope_allows(grant.resource_scope or "*", scope_id)


def _usable(
    session: Session, principal_id: str, permission: str, now: datetime
) -> PermissionGrant | None:
    grant = get_grant(session, principal_id, permission)
    if grant is None or grant.revoked_at is not None:
        return None
    if grant.expires_at is not None and ensure_aware(grant.expires_at) <= now:
        return None
    return grant


def _resource_scope(
    session: Session, principal_id: str, permission: str, delegation_id: str | None
) -> str:
    if delegation_id:
        delegation = session.get(Delegation, delegation_id)
        if delegation is None:
            return ""
        return delegation.resource_scope
    grant = get_grant(session, principal_id, permission)
    if grant is None:
        return ""
    return grant.resource_scope or "*"
