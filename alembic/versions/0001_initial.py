"""Initial Apex task, authority, and evidence schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-08

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
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
)


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Create the foundational tables."""
    op.create_table(
        "principals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("external_key", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_principals"),
        sa.UniqueConstraint("external_key", name="uq_principals_external_key"),
    )
    op.create_table(
        "permission_grants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("principal_id", sa.String(length=36), nullable=False),
        sa.Column("permission_key", sa.String(length=128), nullable=False),
        sa.Column("effect", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "effect IN ('allow', 'deny', 'require_approval')",
            name="ck_permission_grants_effect",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_permission_grants_principal_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_permission_grants"),
        sa.UniqueConstraint(
            "principal_id",
            "permission_key",
            name="uq_permission_grants_principal_permission",
        ),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("principal_id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("objective", sa.Text(), nullable=True),
        sa.Column("outcome_kind", sa.String(length=32), nullable=True),
        sa.Column("project_key", sa.String(length=64), nullable=True),
        sa.Column("intent_json", sa.JSON(), nullable=True),
        sa.Column("result_payload", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_in_clause("status", _TASK_STATUSES), name="ck_tasks_status"),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_tasks_principal_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.UniqueConstraint(
            "principal_id",
            "idempotency_key",
            name="uq_tasks_principal_idempotency",
        ),
    )
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_principal_id", "tasks", ["principal_id"])
    op.create_table(
        "task_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=True),
        sa.Column("actor_principal_id", sa.String(length=36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_principal_id"],
            ["principals.id"],
            name="fk_task_events_actor_principal_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_task_events_task_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_events"),
    )
    op.create_index("ix_task_events_task_created", "task_events", ["task_id", "created_at"])
    op.create_table(
        "plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_plans_task_id", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_plans"),
        sa.UniqueConstraint("task_id", "version", name="uq_plans_task_version"),
    )
    op.create_table(
        "plan_steps",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("input_bindings", sa.JSON(), nullable=False),
        sa.Column("depends_on", sa.JSON(), nullable=False),
        sa.Column("permission_key", sa.String(length=128), nullable=False),
        sa.Column("risk", sa.String(length=32), nullable=False),
        sa.Column("verification_kind", sa.String(length=128), nullable=False),
        sa.Column("failure_policy", sa.JSON(), nullable=False),
        sa.Column("operation_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'failed')",
            name="ck_plan_steps_status",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.id"], name="fk_plan_steps_plan_id", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_plan_steps"),
        sa.UniqueConstraint("plan_id", "step_index", name="uq_plan_steps_plan_index"),
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("plan_step_id", sa.String(length=36), nullable=False),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("operation_hash", sa.String(length=64), nullable=False),
        sa.Column("canonical_payload", sa.JSON(), nullable=False),
        sa.Column("canonical_bindings", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_by_principal_id", sa.String(length=36), nullable=False),
        sa.Column("decided_by_principal_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'cancelled')",
            name="ck_approvals_status",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_principal_id"],
            ["principals.id"],
            name="fk_approvals_decided_by",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["plans.id"], name="fk_approvals_plan_id", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["plan_step_id"],
            ["plan_steps.id"],
            name="fk_approvals_plan_step_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_principal_id"],
            ["principals.id"],
            name="fk_approvals_requested_by",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_approvals_task_id", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_approvals"),
        sa.UniqueConstraint("plan_step_id", "operation_hash", name="uq_approvals_step_hash"),
    )
    op.create_index("ix_approvals_task_id", "approvals", ["task_id"])
    op.create_index("ix_approvals_status", "approvals", ["status"])
    op.create_table(
        "executions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("plan_step_id", sa.String(length=36), nullable=False),
        sa.Column("principal_id", sa.String(length=36), nullable=False),
        sa.Column("approval_id", sa.String(length=36), nullable=True),
        sa.Column("capability_id", sa.String(length=128), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("operation_hash", sa.String(length=64), nullable=False),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("output_payload", sa.JSON(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'timed_out')",
            name="ck_executions_status",
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["approvals.id"],
            name="fk_executions_approval_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_step_id"],
            ["plan_steps.id"],
            name="fk_executions_plan_step_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_executions_principal_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_executions_task_id", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_executions"),
        sa.UniqueConstraint("plan_step_id", "attempt", name="uq_executions_step_attempt"),
    )
    op.create_index("ix_executions_task_id", "executions", ["task_id"])
    op.create_table(
        "verifications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("advisory_note", sa.Text(), nullable=True),
        sa.Column("advisory_suggestion", sa.String(length=32), nullable=True),
        sa.Column("advisory_disagrees", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('passed', 'failed', 'inconclusive')",
            name="ck_verifications_status",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name="fk_verifications_execution_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_verifications_task_id", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_verifications"),
        sa.UniqueConstraint("execution_id", name="uq_verifications_execution_id"),
    )
    op.create_table(
        "context_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=128), nullable=False),
        sa.Column("principal_id", sa.String(length=36), nullable=True),
        sa.Column("trust", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope_type IN ('personal', 'project', 'task', 'system')",
            name="ck_context_records_scope_type",
        ),
        sa.CheckConstraint(
            "trust IN ('proposal', 'trusted')",
            name="ck_context_records_trust",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_context_records_principal_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_context_records"),
    )
    op.create_index("ix_context_records_scope", "context_records", ["scope_type", "scope_id"])


def downgrade() -> None:
    """Drop the foundational tables."""
    op.drop_index("ix_context_records_scope", table_name="context_records")
    op.drop_table("context_records")
    op.drop_table("verifications")
    op.drop_index("ix_executions_task_id", table_name="executions")
    op.drop_table("executions")
    op.drop_index("ix_approvals_status", table_name="approvals")
    op.drop_index("ix_approvals_task_id", table_name="approvals")
    op.drop_table("approvals")
    op.drop_table("plan_steps")
    op.drop_table("plans")
    op.drop_index("ix_task_events_task_created", table_name="task_events")
    op.drop_table("task_events")
    op.drop_index("ix_tasks_principal_id", table_name="tasks")
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_table("tasks")
    op.drop_table("permission_grants")
    op.drop_table("principals")
