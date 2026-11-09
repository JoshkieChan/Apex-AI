"""Create one-hop delegations. A delegate cannot receive authority the delegator lacks."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from apex.authority.engine import CONTROL_PERMISSIONS
from apex.authority.scope import scope_allows
from apex.clock import ensure_aware
from apex.errors import DomainError
from apex.persistence.models import Delegation, Principal
from apex.persistence.repository import get_grant
from apex.runtime_ids import new_id

MAX_DELEGATION_HOURS = 24 * 7


class DelegationService:
    """Record a narrowed copy of one direct grant."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        delegator: Principal,
        delegate: Principal,
        permission_key: str,
        resource_scope: str,
        expires_at: datetime,
        now: datetime,
    ) -> Delegation:
        if delegator.id == delegate.id:
            raise DomainError("delegation_forbidden", "A principal cannot delegate to itself.")
        if permission_key in CONTROL_PERMISSIONS:
            raise DomainError(
                "delegation_forbidden",
                "Control-plane permissions cannot be delegated.",
            )
        grant = get_grant(self._session, delegator.id, permission_key)
        if grant is None or grant.revoked_at is not None or grant.effect == "deny":
            raise DomainError(
                "delegation_forbidden",
                "The delegator does not hold that permission.",
            )
        if grant.expires_at is not None and ensure_aware(grant.expires_at) <= now:
            raise DomainError("delegation_forbidden", "The delegator's grant has expired.")
        if ensure_aware(expires_at) <= now or ensure_aware(expires_at) > now + timedelta(
            hours=MAX_DELEGATION_HOURS
        ):
            raise DomainError("delegation_forbidden", "The delegation expiry is not valid.")
        if grant.expires_at is not None and ensure_aware(expires_at) > ensure_aware(
            grant.expires_at
        ):
            raise DomainError(
                "delegation_forbidden",
                "A delegation cannot outlive the delegator's grant.",
            )
        scope = resource_scope or "*"
        grant_scope = grant.resource_scope or "*"
        try:
            narrowed = scope_allows(grant_scope, scope)
        except DomainError as exc:
            raise DomainError(
                "delegation_forbidden",
                "A delegation cannot widen the delegator's scope.",
            ) from exc
        if not narrowed:
            raise DomainError(
                "delegation_forbidden",
                "A delegation cannot widen the delegator's scope.",
            )
        existing = self._session.scalar(
            select(Delegation).where(
                Delegation.delegator_principal_id == delegator.id,
                Delegation.delegate_principal_id == delegate.id,
                Delegation.permission_key == permission_key,
                Delegation.revoked_at.is_(None),
            )
        )
        if existing is not None and ensure_aware(existing.expires_at) > now:
            raise DomainError("delegation_exists", "An active delegation already exists.")
        record = Delegation(
            id=new_id(),
            delegator_principal_id=delegator.id,
            delegate_principal_id=delegate.id,
            permission_key=permission_key,
            resource_scope=scope,
            expires_at=expires_at,
            revoked_at=None,
            created_at=now,
        )
        self._session.add(record)
        self._session.flush()
        return record
