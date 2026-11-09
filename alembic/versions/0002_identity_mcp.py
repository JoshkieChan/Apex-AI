"""Identity links, delegations, MCP registration, and uncertain execution.

Revision ID: 0002_identity_mcp
Revises: 0001_initial
Create Date: 2026-11-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_identity_mcp"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TASK_STATUSES = (
    "submitted",
    "interpreting",
    "planned",
    "awaiting_approval",
    "executing",
    "verifying",
    "completed",
    "failed",
    "cancelled",
    "uncertain",
)
_EXECUTION_STATUSES = ("running", "succeeded", "failed", "timed_out", "uncertain")


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Add identity, delegation, and MCP registration tables."""
    op.create_table(
        "identity_subjects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("principal_id", sa.String(length=36), nullable=False),
        sa.Column("issuer", sa.String(length=256), nullable=False),
        sa.Column("subject", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_identity_subjects_principal_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer", "subject", name="uq_identity_subjects_issuer_subject"),
    )
    op.create_index("ix_identity_subjects_principal_id", "identity_subjects", ["principal_id"])
    op.create_table(
        "delegations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("delegator_principal_id", sa.String(length=36), nullable=False),
        sa.Column("delegate_principal_id", sa.String(length=36), nullable=False),
        sa.Column("permission_key", sa.String(length=128), nullable=False),
        sa.Column("resource_scope", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "delegator_principal_id <> delegate_principal_id",
            name="no_self",
        ),
        sa.ForeignKeyConstraint(
            ["delegator_principal_id"],
            ["principals.id"],
            name="fk_delegations_delegator",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["delegate_principal_id"],
            ["principals.id"],
            name="fk_delegations_delegate",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_delegations_delegate", "delegations", ["delegate_principal_id"])
    op.add_column(
        "permission_grants",
        sa.Column("resource_scope", sa.String(length=128), nullable=False, server_default="*"),
    )
    op.add_column(
        "permission_grants",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "permission_grants",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "plan_steps",
        sa.Column("capability_version", sa.String(length=32), nullable=False, server_default="1"),
    )
    op.add_column(
        "plan_steps",
        sa.Column("schema_hash", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "approvals",
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "executions",
        sa.Column("certainty", sa.String(length=16), nullable=False, server_default="definite"),
    )
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("delegation_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_tasks_delegation_id",
            "delegations",
            ["delegation_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("server_key", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("transport", sa.String(length=32), nullable=False),
        sa.Column("trust_status", sa.String(length=32), nullable=False),
        sa.Column("owner_principal_id", sa.String(length=36), nullable=False),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "trust_status IN ('unreviewed', 'reviewed', 'revoked')",
            name="trust_status",
        ),
        sa.ForeignKeyConstraint(
            ["owner_principal_id"],
            ["principals.id"],
            name="fk_mcp_servers_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("server_key", name="uq_mcp_servers_server_key"),
    )
    op.create_table(
        "mcp_tools",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("server_id", sa.String(length=36), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("capability_id", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("risk", sa.String(length=32), nullable=True),
        sa.Column("permission_key", sa.String(length=128), nullable=True),
        sa.Column("verification_kind", sa.String(length=128), nullable=True),
        sa.Column("registration_state", sa.String(length=32), nullable=False),
        sa.Column("schema_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("registered_by_principal_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "registration_state IN ('discovered', 'registered', 'disabled', 'revoked')",
            name="registration_state",
        ),
        sa.ForeignKeyConstraint(
            ["server_id"], ["mcp_servers.id"], name="fk_mcp_tools_server_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["registered_by_principal_id"],
            ["principals.id"],
            name="fk_mcp_tools_registered_by",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("server_id", "tool_name", name="uq_mcp_tools_server_tool"),
        sa.UniqueConstraint("capability_id", name="uq_mcp_tools_capability_id"),
    )
    op.create_table(
        "capability_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("capability_id", sa.String(length=256), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("schema_hash", sa.String(length=64), nullable=False),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "capability_id", "version", name="uq_capability_versions_capability_version"
        ),
    )
    op.create_table(
        "registration_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("mcp_tool_id", sa.String(length=36), nullable=False),
        sa.Column("actor_principal_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["mcp_tool_id"],
            ["mcp_tools.id"],
            name="fk_registration_events_tool",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_principal_id"],
            ["principals.id"],
            name="fk_registration_events_actor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_registration_events_tool",
        "registration_events",
        ["mcp_tool_id", "created_at"],
    )
    _replace_check("tasks", "ck_tasks_status", _in_clause("status", _TASK_STATUSES))
    _replace_check("executions", "ck_executions_status", _in_clause("status", _EXECUTION_STATUSES))


def downgrade() -> None:
    """Remove milestone 2 tables and columns."""
    _replace_check(
        "executions",
        "ck_executions_status",
        "status IN ('running', 'succeeded', 'failed', 'timed_out')",
    )
    _replace_check(
        "tasks",
        "ck_tasks_status",
        "status IN ('submitted', 'interpreting', 'planned', 'awaiting_approval', "
        "'executing', 'verifying', 'completed', 'failed', 'cancelled')",
    )
    op.drop_index("ix_registration_events_tool", table_name="registration_events")
    op.drop_table("registration_events")
    op.drop_table("capability_versions")
    op.drop_table("mcp_tools")
    op.drop_table("mcp_servers")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_constraint("fk_tasks_delegation_id", type_="foreignkey")
        batch.drop_column("delegation_id")
    op.drop_column("executions", "certainty")
    op.drop_column("approvals", "consumed_at")
    op.drop_column("plan_steps", "schema_hash")
    op.drop_column("plan_steps", "capability_version")
    op.drop_column("permission_grants", "revoked_at")
    op.drop_column("permission_grants", "expires_at")
    op.drop_column("permission_grants", "resource_scope")
    op.drop_index("ix_delegations_delegate", table_name="delegations")
    op.drop_table("delegations")
    op.drop_index("ix_identity_subjects_principal_id", table_name="identity_subjects")
    op.drop_table("identity_subjects")


def _replace_check(table: str, name: str, expression: str) -> None:
    with op.batch_alter_table(table) as batch:
        batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(name, expression)
