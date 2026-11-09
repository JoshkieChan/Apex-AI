"""Goal leases, verification records, and memory purge tombstones.

Revision ID: 0006_reliability
Revises: 0005_coordination
Create Date: 2026-11-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_reliability"
down_revision: str | None = "0005_coordination"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERIFY = (
    "status IN ('passed', 'failed', 'inconclusive', 'partially_verified', 'not_applicable')"
)


def upgrade() -> None:
    """Add coordination leases, verification decisions, and purge tombstones."""
    with op.batch_alter_table("goals") as batch:
        batch.add_column(sa.Column("lease_owner", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("recovery_attempts", sa.Integer(), nullable=False, server_default="0")
        )
    op.create_index("ix_goals_lease", "goals", ["status", "lease_expires_at"])
    op.create_table(
        "goal_verifications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("goal_id", sa.String(length=36), nullable=False),
        sa.Column("subtask_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("failure_reason", sa.String(length=128), nullable=True),
        sa.Column("verifier", sa.String(length=64), nullable=False),
        sa.Column("shared_model", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["goal_id"], ["goals.id"], name="fk_goal_verifications_goal", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["subtask_id"],
            ["goal_subtasks.id"],
            name="fk_goal_verifications_subtask",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(_VERIFY, name="ck_goal_verifications_status"),
    )
    op.create_index("ix_goal_verifications_goal", "goal_verifications", ["goal_id", "revision"])
    op.create_table(
        "memory_purges",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("memory_id", sa.String(length=36), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column("revision_count", sa.Integer(), nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_purges_scope", "memory_purges", ["scope_type", "scope_id"])


def downgrade() -> None:
    """Remove reliability columns and tables."""
    op.drop_index("ix_memory_purges_scope", table_name="memory_purges")
    op.drop_table("memory_purges")
    op.drop_index("ix_goal_verifications_goal", table_name="goal_verifications")
    op.drop_table("goal_verifications")
    op.drop_index("ix_goals_lease", table_name="goals")
    with op.batch_alter_table("goals") as batch:
        batch.drop_column("recovery_attempts")
        batch.drop_column("lease_expires_at")
        batch.drop_column("lease_owner")
