"""Logical agents, goals, and plan revisions.

Revision ID: 0005_coordination
Revises: 0004_memory
Create Date: 2026-11-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_coordination"
down_revision: str | None = "0004_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AGENT = "status IN ('draft', 'registered', 'active', 'disabled', 'retired')"
_GOAL = (
    "status IN ('submitted', 'planned', 'running', 'awaiting_approval', 'completed', "
    "'partial', 'failed', 'cancelled', 'uncertain')"
)
_SUBTASK = (
    "status IN ('pending', 'ready', 'running', 'completed', 'failed', 'blocked', "
    "'cancelled', 'uncertain', 'awaiting_approval')"
)


def upgrade() -> None:
    """Add logical agents and coordinated goal state."""
    op.create_table(
        "logical_agents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_key", sa.String(length=64), nullable=False),
        sa.Column("agent_type", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("allowed_permissions", sa.JSON(), nullable=False),
        sa.Column("autonomy", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_key", name="uq_logical_agents_key"),
        sa.CheckConstraint(_AGENT, name="ck_logical_agents_status"),
    )
    op.create_table(
        "goals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("principal_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("original_request", sa.Text(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("project_key", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("autonomy", sa.String(length=32), nullable=False),
        sa.Column("allow_partial", sa.Boolean(), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["principal_id"], ["principals.id"], name="fk_goals_principal", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name="fk_goals_task", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(_GOAL, name="ck_goals_status"),
    )
    op.create_index("ix_goals_principal", "goals", ["principal_id"])
    op.create_table(
        "goal_subtasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("goal_id", sa.String(length=36), nullable=False),
        sa.Column("subtask_key", sa.String(length=64), nullable=False),
        sa.Column("agent_key", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("capability_id", sa.String(length=128), nullable=True),
        sa.Column("permission_key", sa.String(length=128), nullable=True),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("depends_on", sa.JSON(), nullable=False),
        sa.Column("acceptance", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("plan_step_id", sa.String(length=36), nullable=True),
        sa.Column("execution_id", sa.String(length=36), nullable=True),
        sa.Column("output_payload", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["goal_id"], ["goals.id"], name="fk_goal_subtasks_goal", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("goal_id", "subtask_key", name="uq_goal_subtasks_key"),
        sa.CheckConstraint(_SUBTASK, name="ck_goal_subtasks_status"),
    )
    op.create_index("ix_goal_subtasks_goal", "goal_subtasks", ["goal_id"])
    op.create_table(
        "agent_invocations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("goal_id", sa.String(length=36), nullable=False),
        sa.Column("subtask_id", sa.String(length=36), nullable=False),
        sa.Column("agent_key", sa.String(length=64), nullable=False),
        sa.Column("principal_id", sa.String(length=36), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("output_payload", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["goal_id"], ["goals.id"], name="fk_agent_invocations_goal", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["subtask_id"],
            ["goal_subtasks.id"],
            name="fk_agent_invocations_subtask",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_agent_invocations_principal",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subtask_id", "attempt", name="uq_agent_invocations_attempt"),
    )
    op.create_index("ix_agent_invocations_goal", "agent_invocations", ["goal_id"])
    op.create_table(
        "plan_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("goal_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("trigger", sa.String(length=64), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["goal_id"], ["goals.id"], name="fk_plan_revisions_goal", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("goal_id", "revision", name="uq_plan_revisions_goal_revision"),
    )
    op.create_table(
        "goal_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("goal_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor_principal_id", sa.String(length=36), nullable=True),
        sa.Column("agent_key", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["goal_id"], ["goals.id"], name="fk_goal_events_goal", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["actor_principal_id"],
            ["principals.id"],
            name="fk_goal_events_actor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_goal_events_goal", "goal_events", ["goal_id", "created_at"])


def downgrade() -> None:
    """Remove coordinated goal state."""
    op.drop_index("ix_goal_events_goal", table_name="goal_events")
    op.drop_table("goal_events")
    op.drop_table("plan_revisions")
    op.drop_index("ix_agent_invocations_goal", table_name="agent_invocations")
    op.drop_table("agent_invocations")
    op.drop_index("ix_goal_subtasks_goal", table_name="goal_subtasks")
    op.drop_table("goal_subtasks")
    op.drop_index("ix_goals_principal", table_name="goals")
    op.drop_table("goals")
    op.drop_table("logical_agents")
