"""Governed memory, revisions, and remote reconciliation references.

Revision ID: 0004_memory
Revises: 0003_external_trust
Create Date: 2026-11-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_memory"
down_revision: str | None = "0003_external_trust"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCOPE = "scope_type IN ('personal', 'project', 'task', 'system')"
_CATEGORY = (
    "category IN ('fact', 'preference', 'decision', 'constraint', 'procedure', "
    "'observation', 'event', 'hypothesis', 'verified_outcome')"
)
_RELIABILITY = (
    "reliability IN ('unverified', 'observed', 'inferred', 'verified', 'disputed', 'superseded')"
)
_LIFECYCLE = "lifecycle IN ('candidate', 'active', 'superseded', 'expired', 'deleted')"
_SOURCE = "source_type IN ('user', 'tool', 'model', 'task', 'system')"


def upgrade() -> None:
    """Add governed memory and a reconciliation capability reference."""
    op.create_table(
        "memories",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column("owner_principal_id", sa.String(length=36), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(length=128), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("reliability", sa.String(length=32), nullable=False),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("supersedes_id", sa.String(length=36), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("source_principal_id", sa.String(length=36), nullable=True),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("verification_method", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_SCOPE, name="ck_memories_scope"),
        sa.CheckConstraint(_CATEGORY, name="ck_memories_category"),
        sa.CheckConstraint(_RELIABILITY, name="ck_memories_reliability"),
        sa.CheckConstraint(_LIFECYCLE, name="ck_memories_lifecycle"),
        sa.CheckConstraint(_SOURCE, name="ck_memories_source"),
        sa.ForeignKeyConstraint(
            ["owner_principal_id"], ["principals.id"], name="fk_memories_owner", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"], ["memories.id"], name="fk_memories_supersedes", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_principal_id"],
            ["principals.id"],
            name="fk_memories_source_principal",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_memories_scope_lifecycle", "memories", ["scope_type", "scope_id", "lifecycle"]
    )
    op.create_index("ix_memories_owner", "memories", ["owner_principal_id"])
    op.create_table(
        "memory_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("memory_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("reliability", sa.String(length=32), nullable=False),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.Column("actor_principal_id", sa.String(length=36), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["memory_id"], ["memories.id"], name="fk_memory_revisions_memory", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["actor_principal_id"],
            ["principals.id"],
            name="fk_memory_revisions_actor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("memory_id", "version", name="uq_memory_revisions_version"),
    )
    op.create_index("ix_memory_revisions_memory", "memory_revisions", ["memory_id"])
    op.create_table(
        "memory_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("memory_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("actor_principal_id", sa.String(length=36), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["memory_id"], ["memories.id"], name="fk_memory_events_memory", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["actor_principal_id"],
            ["principals.id"],
            name="fk_memory_events_actor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_events_memory", "memory_events", ["memory_id", "created_at"])
    with op.batch_alter_table("mcp_tools") as batch:
        batch.add_column(
            sa.Column("reconciles_capability_id", sa.String(length=256), nullable=True)
        )


def downgrade() -> None:
    """Remove governed memory and the reconciliation reference."""
    with op.batch_alter_table("mcp_tools") as batch:
        batch.drop_column("reconciles_capability_id")
    op.drop_index("ix_memory_events_memory", table_name="memory_events")
    op.drop_table("memory_events")
    op.drop_index("ix_memory_revisions_memory", table_name="memory_revisions")
    op.drop_table("memory_revisions")
    op.drop_index("ix_memories_owner", table_name="memories")
    op.drop_index("ix_memories_scope_lifecycle", table_name="memories")
    op.drop_table("memories")
