"""Production readiness includes the Alembic revision this process was built against."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from apex.config import Settings

SCHEMA_REVISION = "0008_connectors"


def schema_compatible(session: Session, settings: Settings) -> bool:
    """Development and test databases may be created from the models. Production may not."""
    if settings.environment != "production":
        return True
    try:
        version = session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        session.rollback()
        return False
    return version == SCHEMA_REVISION
