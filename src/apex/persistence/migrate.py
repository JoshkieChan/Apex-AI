"""Apply database migrations once. API processes do not migrate on startup."""

from __future__ import annotations

from alembic import command
from alembic.config import Config


def main() -> None:
    """Upgrade the database in APEX_DATABASE_URL to the current revision."""
    command.upgrade(Config("alembic.ini"), "head")


if __name__ == "__main__":
    main()
