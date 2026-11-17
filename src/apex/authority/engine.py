"""Default-deny permission evaluation. Grants are data, not model output."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from apex.clock import ensure_aware, utcnow
from apex.contracts.schemas import Decision
from apex.persistence.models import Delegation, PermissionGrant
from apex.persistence.repository import get_grant

CONTROL_PERMISSIONS = frozenset(
    {
        "authority.approve",
        "authority.grant",
        "capability.register",
        "operations.reconcile",
        "operations.suspend",
        "memory.manage",
        "memory.verify",
    }
)


class AuthorityEngine:
    """Evaluate one principal against one permission key."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def evaluate(
        self,
        principal_id: str,
        permission_key: str,
        *,
        now: datetime | None = None,
        delegation_id: str | None = None,
        risk: str | None = None,
    ) -> Decision:
        moment = now or utcnow()
        if delegation_id:
            decision = self._delegated(principal_id, permission_key, delegation_id, moment)
        else:
            decision = self._direct(principal_id, permission_key, moment)
        if decision == Decision.allow and risk == "high":
            return Decision.require_approval
        return decision

    def can_approve(self, approver_id: str, permission_key: str, risk: str, now: datetime) -> bool:
        """An approver needs an allow grant whose scope covers this operation."""
        grant = self._usable_grant(approver_id, "authority.approve", now)
        if grant is None or grant.effect != Decision.allow.value:
            return False
        scope = grant.resource_scope or "*"
        if scope == "*":
            return True
        return scope in {permission_key, risk}

    def _direct(self, principal_id: str, permission_key: str, now: datetime) -> Decision:
        grant = self._usable_grant(principal_id, permission_key, now)
        if grant is None:
            return Decision.deny
        if grant.effect == Decision.allow.value:
            return Decision.allow
        if grant.effect == Decision.require_approval.value:
            return Decision.require_approval
        return Decision.deny

    def _delegated(
        self,
        principal_id: str,
        permission_key: str,
        delegation_id: str,
        now: datetime,
    ) -> Decision:
        delegation = self._session.get(Delegation, delegation_id)
        if delegation is None or delegation.delegate_principal_id != principal_id:
            return Decision.deny
        if delegation.permission_key != permission_key:
            return Decision.deny
        if delegation.revoked_at is not None or ensure_aware(delegation.expires_at) <= now:
            return Decision.deny
        if permission_key in CONTROL_PERMISSIONS:
            return Decision.deny
        # The delegator's current direct grant caps the delegation. Chains are not consulted.
        return self._direct(delegation.delegator_principal_id, permission_key, now)

    def _usable_grant(
        self, principal_id: str, permission_key: str, now: datetime
    ) -> PermissionGrant | None:
        grant = get_grant(self._session, principal_id, permission_key)
        if grant is None or grant.revoked_at is not None:
            return None
        if grant.expires_at is not None and ensure_aware(grant.expires_at) <= now:
            return None
        return grant
