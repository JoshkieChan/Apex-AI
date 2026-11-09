"""Remote MCP trust metadata, operation certainty, and reconciliation evidence.

Revision ID: 0003_external_trust
Revises: 0002_identity_mcp
Create Date: 2026-11-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_external_trust"
down_revision: str | None = "0002_identity_mcp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REGISTRATION = (
    "registration_state IN ('discovered', 'registered', 'disabled', 'revoked', 'schema_changed')"
)
_REGISTRATION_PREVIOUS = "registration_state IN ('discovered', 'registered', 'disabled', 'revoked')"
_CERTAINTY = (
    "outcome_certainty IN ('not_sent', 'possibly_sent', 'confirmed_success', "
    "'confirmed_failure', 'inconclusive')"
)


def upgrade() -> None:
    """Add remote trust fields, certainty, and reconciliation attempts."""
    with op.batch_alter_table("executions") as batch:
        batch.add_column(
            sa.Column(
                "outcome_certainty",
                sa.String(length=32),
                nullable=False,
                server_default="not_sent",
            )
        )
        batch.add_column(sa.Column("external_operation_id", sa.String(length=128), nullable=True))
        batch.add_column(
            sa.Column(
                "capability_version",
                sa.String(length=32),
                nullable=False,
                server_default="",
            )
        )
        batch.add_column(
            sa.Column("schema_hash", sa.String(length=64), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("reconcile_owner", sa.String(length=128), nullable=True))
        batch.add_column(
            sa.Column("reconcile_lease_expires", sa.DateTime(timezone=True), nullable=True)
        )
    op.execute(
        "UPDATE executions SET outcome_certainty = 'confirmed_success' WHERE status = 'succeeded'"
    )
    op.execute(
        "UPDATE executions SET outcome_certainty = 'confirmed_failure' "
        "WHERE status IN ('failed', 'timed_out')"
    )
    op.execute(
        "UPDATE executions SET outcome_certainty = 'possibly_sent' "
        "WHERE status IN ('running', 'uncertain')"
    )
    with op.batch_alter_table("executions") as batch:
        batch.create_check_constraint("ck_executions_outcome_certainty", _CERTAINTY)
        batch.create_index("ix_executions_outcome_certainty", ["outcome_certainty"])
    with op.batch_alter_table("mcp_servers") as batch:
        batch.add_column(sa.Column("endpoint", sa.Text(), nullable=True))
        batch.add_column(sa.Column("credential_ref", sa.String(length=256), nullable=True))
        batch.add_column(
            sa.Column("tls_required", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    _replace_check("mcp_tools", "registration_state", _REGISTRATION)
    op.create_table(
        "reconciliation_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("actor_principal_id", sa.String(length=36), nullable=False),
        sa.Column("certainty_before", sa.String(length=32), nullable=False),
        sa.Column("certainty_after", sa.String(length=32), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name="fk_reconciliation_execution",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_principal_id"],
            ["principals.id"],
            name="fk_reconciliation_actor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reconciliation_attempts_execution",
        "reconciliation_attempts",
        ["execution_id", "created_at"],
    )


def downgrade() -> None:
    """Remove reconciliation records and remote trust columns."""
    op.drop_index("ix_reconciliation_attempts_execution", table_name="reconciliation_attempts")
    op.drop_table("reconciliation_attempts")
    op.execute(
        "UPDATE mcp_tools SET registration_state = 'disabled' "
        "WHERE registration_state = 'schema_changed'"
    )
    _replace_check("mcp_tools", "registration_state", _REGISTRATION_PREVIOUS)
    with op.batch_alter_table("mcp_servers") as batch:
        batch.drop_column("tls_required")
        batch.drop_column("credential_ref")
        batch.drop_column("endpoint")
    with op.batch_alter_table("executions") as batch:
        batch.drop_index("ix_executions_outcome_certainty")
        batch.drop_constraint("ck_executions_outcome_certainty", type_="check")
        batch.drop_column("reconcile_lease_expires")
        batch.drop_column("reconcile_owner")
        batch.drop_column("schema_hash")
        batch.drop_column("capability_version")
        batch.drop_column("external_operation_id")
        batch.drop_column("outcome_certainty")


def _replace_check(table: str, name: str, expression: str) -> None:
    with op.batch_alter_table(table) as batch:
        batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(name, expression)
