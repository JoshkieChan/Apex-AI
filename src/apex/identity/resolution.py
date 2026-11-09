"""Resolve a validated subject to an internal principal. The caller does not supply the id."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from apex.persistence.models import IdentitySubject, Principal


def principal_for_subject(session: Session, issuer: str, subject: str) -> Principal | None:
    """Return the principal linked to this issuer and subject."""
    link = session.scalar(
        select(IdentitySubject).where(
            IdentitySubject.issuer == issuer,
            IdentitySubject.subject == subject,
        )
    )
    if link is None:
        return None
    return session.get(Principal, link.principal_id)
