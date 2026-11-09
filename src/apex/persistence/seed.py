"""Development principals. This is not an authentication system."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from apex.persistence.models import IdentitySubject, PermissionGrant, Principal
from apex.persistence.repository import get_grant, get_principal_by_key
from apex.runtime_ids import new_id

DEV_PRINCIPALS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "dev-user",
        "Development User",
        (
            ("text.analyze", "allow"),
            ("records.export", "require_approval"),
            ("records.lookup", "allow"),
            ("records.update", "require_approval"),
            ("operations.reconcile", "allow"),
            ("memory.read", "allow"),
            ("memory.create", "allow"),
            ("memory.update", "allow"),
            ("memory.delete", "allow"),
        ),
    ),
    ("dev-restricted", "Restricted User", ()),
    (
        "dev-approver",
        "Development Approver",
        (
            ("authority.approve", "allow"),
            ("authority.grant", "allow"),
            ("text.analyze", "allow"),
            ("memory.read", "allow"),
            ("memory.manage", "allow"),
            ("memory.verify", "allow"),
        ),
    ),
    ("dev-registrar", "Development Registrar", (("capability.register", "allow"),)),
    ("dev-denied", "Explicit Deny", (("text.analyze", "deny"),)),
)


def seed_development_data(
    session: Session,
    *,
    now: datetime | None = None,
    issuer: str = "",
) -> None:
    """Create or update the documented local principals and their grants."""
    moment = now or datetime.now().astimezone()
    for external_key, display_name, grants in DEV_PRINCIPALS:
        principal = get_principal_by_key(session, external_key)
        if principal is None:
            principal = Principal(
                id=new_id(),
                external_key=external_key,
                display_name=display_name,
                kind="user",
                created_at=moment,
            )
            session.add(principal)
            session.flush()
        for permission_key, effect in grants:
            existing = get_grant(session, principal.id, permission_key)
            if existing is None:
                session.add(
                    PermissionGrant(
                        id=new_id(),
                        principal_id=principal.id,
                        permission_key=permission_key,
                        effect=effect,
                        created_at=moment,
                    )
                )
            else:
                existing.effect = effect
        if issuer:
            _link_subject(session, principal, issuer, external_key, moment)
    session.flush()


def _link_subject(
    session: Session,
    principal: Principal,
    issuer: str,
    subject: str,
    now: datetime,
) -> None:
    existing = session.scalar(
        select(IdentitySubject).where(
            IdentitySubject.issuer == issuer,
            IdentitySubject.subject == subject,
        )
    )
    if existing is None:
        session.add(
            IdentitySubject(
                id=new_id(),
                principal_id=principal.id,
                issuer=issuer,
                subject=subject,
                created_at=now,
            )
        )
