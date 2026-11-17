"""Operator execution hold.

Revision ID: 0009_controls
Revises: 0008_connectors
Create Date: 2026-11-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_controls"
down_revision: str | None = "0008_connectors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the execution hold. A missing row leaves execution open."""
    op.create_table(
        "runtime_controls",
        sa.Column("control_key", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("control_key", name="pk_runtime_controls"),
    )


def downgrade() -> None:
    """Remove the execution hold."""
    op.drop_table("runtime_controls")
