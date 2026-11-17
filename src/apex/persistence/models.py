"""Relational model for the Apex task lifecycle.

JSON columns hold extensible payloads whose shapes are validated by Pydantic
before they are stored. Identity, state, and relationships are real columns.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from apex.persistence.base import Base
from apex.persistence.types import UtcDateTime

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
_APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired", "cancelled")
_EXECUTION_STATUSES = ("running", "succeeded", "failed", "timed_out", "uncertain")
_VERIFICATION_STATUSES = ("passed", "failed", "inconclusive")
_SCOPES = ("personal", "project", "task", "system")
_TRUST = ("proposal", "trusted")
_STEP_STATUSES = ("pending", "completed", "failed")


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({rendered})"


class Principal(Base):
    __tablename__ = "principals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    external_key: Mapped[str] = mapped_column(String(128), unique=True)
    display_name: Mapped[str] = mapped_column(String(256))
    kind: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class PermissionGrant(Base):
    __tablename__ = "permission_grants"
    __table_args__ = (
        UniqueConstraint(
            "principal_id",
            "permission_key",
            name="uq_permission_grants_principal_permission",
        ),
        CheckConstraint(
            "effect IN ('allow', 'deny', 'require_approval')",
            name="effect",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_permission_grants_principal_id")
    )
    permission_key: Mapped[str] = mapped_column(String(128))
    effect: Mapped[str] = mapped_column(String(32))
    resource_scope: Mapped[str] = mapped_column(String(128), default="*")
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True, default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("principal_id", "idempotency_key", name="uq_tasks_principal_idempotency"),
        CheckConstraint(_in_clause("status", _TASK_STATUSES), name="status"),
        Index("ix_tasks_status", "status"),
        Index("ix_tasks_principal_id", "principal_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_tasks_principal_id")
    )
    request_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    objective: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    project_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    intent_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer)
    delegation_id: Mapped[str | None] = mapped_column(
        ForeignKey("delegations.id", ondelete="RESTRICT", name="fk_tasks_delegation_id"),
        nullable=True,
        default=None,
    )
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())


class TaskEvent(Base):
    __tablename__ = "task_events"
    __table_args__ = (Index("ix_task_events_task_created", "task_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT", name="fk_task_events_task_id")
    )
    event_type: Mapped[str] = mapped_column(String(64))
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_principal_id: Mapped[str | None] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_task_events_actor_principal_id"),
        nullable=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (UniqueConstraint("task_id", "version", name="uq_plans_task_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT", name="fk_plans_task_id")
    )
    version: Mapped[int] = mapped_column(Integer)
    objective: Mapped[str] = mapped_column(Text)
    plan_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class PlanStep(Base):
    __tablename__ = "plan_steps"
    __table_args__ = (
        UniqueConstraint("plan_id", "step_index", name="uq_plan_steps_plan_index"),
        CheckConstraint(_in_clause("status", _STEP_STATUSES), name="status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="RESTRICT", name="fk_plan_steps_plan_id")
    )
    step_index: Mapped[int] = mapped_column(Integer)
    capability_id: Mapped[str] = mapped_column(String(128))
    objective: Mapped[str] = mapped_column(Text)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    input_bindings: Mapped[dict[str, Any]] = mapped_column(JSON)
    depends_on: Mapped[list[int]] = mapped_column(JSON)
    permission_key: Mapped[str] = mapped_column(String(128))
    risk: Mapped[str] = mapped_column(String(32))
    verification_kind: Mapped[str] = mapped_column(String(128))
    capability_version: Mapped[str] = mapped_column(String(32), default="1")
    schema_hash: Mapped[str] = mapped_column(String(64), default="")
    failure_policy: Mapped[dict[str, Any]] = mapped_column(JSON)
    operation_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("plan_step_id", "operation_hash", name="uq_approvals_step_hash"),
        CheckConstraint(_in_clause("status", _APPROVAL_STATUSES), name="status"),
        Index("ix_approvals_task_id", "task_id"),
        Index("ix_approvals_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT", name="fk_approvals_task_id")
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="RESTRICT", name="fk_approvals_plan_id")
    )
    plan_step_id: Mapped[str] = mapped_column(
        ForeignKey("plan_steps.id", ondelete="RESTRICT", name="fk_approvals_plan_step_id")
    )
    capability_id: Mapped[str] = mapped_column(String(128))
    operation_hash: Mapped[str] = mapped_column(String(64))
    canonical_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    canonical_bindings: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32))
    requested_by_principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_approvals_requested_by")
    )
    decided_by_principal_id: Mapped[str | None] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_approvals_decided_by"),
        nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True, default=None)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime())
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class Execution(Base):
    __tablename__ = "executions"
    __table_args__ = (
        UniqueConstraint("plan_step_id", "attempt", name="uq_executions_step_attempt"),
        CheckConstraint(_in_clause("status", _EXECUTION_STATUSES), name="status"),
        CheckConstraint(
            "outcome_certainty IN ('not_sent', 'possibly_sent', 'confirmed_success', "
            "'confirmed_failure', 'inconclusive')",
            name="ck_executions_outcome_certainty",
        ),
        Index("ix_executions_task_id", "task_id"),
        Index("ix_executions_outcome_certainty", "outcome_certainty"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT", name="fk_executions_task_id")
    )
    plan_step_id: Mapped[str] = mapped_column(
        ForeignKey("plan_steps.id", ondelete="RESTRICT", name="fk_executions_plan_step_id")
    )
    principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_executions_principal_id")
    )
    approval_id: Mapped[str | None] = mapped_column(
        ForeignKey("approvals.id", ondelete="RESTRICT", name="fk_executions_approval_id"),
        nullable=True,
    )
    capability_id: Mapped[str] = mapped_column(String(128))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    certainty: Mapped[str] = mapped_column(String(16), default="definite")
    outcome_certainty: Mapped[str] = mapped_column(String(32), default="not_sent")
    external_operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capability_version: Mapped[str] = mapped_column(String(32), default="")
    schema_hash: Mapped[str] = mapped_column(String(64), default="")
    reconcile_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reconcile_lease_expires: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    operation_hash: Mapped[str] = mapped_column(String(64))
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class Verification(Base):
    __tablename__ = "verifications"
    __table_args__ = (CheckConstraint(_in_clause("status", _VERIFICATION_STATUSES), name="status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("executions.id", ondelete="RESTRICT", name="fk_verifications_execution_id"),
        unique=True,
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT", name="fk_verifications_task_id")
    )
    status: Mapped[str] = mapped_column(String(32))
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    summary: Mapped[str] = mapped_column(Text)
    advisory_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    advisory_suggestion: Mapped[str | None] = mapped_column(String(32), nullable=True)
    advisory_disagrees: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class ContextRecord(Base):
    __tablename__ = "context_records"
    __table_args__ = (
        CheckConstraint(_in_clause("scope_type", _SCOPES), name="scope_type"),
        CheckConstraint(_in_clause("trust", _TRUST), name="trust"),
        Index("ix_context_records_scope", "scope_type", "scope_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(32))
    scope_id: Mapped[str] = mapped_column(String(128))
    principal_id: Mapped[str | None] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_context_records_principal_id"),
        nullable=True,
    )
    trust: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(64))
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class IdentitySubject(Base):
    """Maps a validated issuer and subject to one internal principal."""

    __tablename__ = "identity_subjects"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_identity_subjects_issuer_subject"),
        Index("ix_identity_subjects_principal_id", "principal_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_identity_subjects_principal_id")
    )
    issuer: Mapped[str] = mapped_column(String(256))
    subject: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class Delegation(Base):
    """One-hop grant of a single permission. It cannot widen the delegator's authority."""

    __tablename__ = "delegations"
    __table_args__ = (
        CheckConstraint("delegator_principal_id <> delegate_principal_id", name="no_self"),
        Index("ix_delegations_delegate", "delegate_principal_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    delegator_principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_delegations_delegator")
    )
    delegate_principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_delegations_delegate")
    )
    permission_key: Mapped[str] = mapped_column(String(128))
    resource_scope: Mapped[str] = mapped_column(String(128), default="*")
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class McpServerRecord(Base):
    """Reviewed MCP server. Connection secrets are not stored here."""

    __tablename__ = "mcp_servers"
    __table_args__ = (
        UniqueConstraint("server_key", name="uq_mcp_servers_server_key"),
        CheckConstraint(
            "trust_status IN ('unreviewed', 'reviewed', 'revoked')",
            name="trust_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    server_key: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text, default="")
    transport: Mapped[str] = mapped_column(String(32))
    endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    credential_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    tls_required: Mapped[bool] = mapped_column(Boolean, default=False)
    trust_status: Mapped[str] = mapped_column(String(32), default="unreviewed")
    owner_principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_mcp_servers_owner")
    )
    last_validated_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())


class McpToolRecord(Base):
    """A discovered tool. It stays unavailable until Apex registers it."""

    __tablename__ = "mcp_tools"
    __table_args__ = (
        UniqueConstraint("server_id", "tool_name", name="uq_mcp_tools_server_tool"),
        UniqueConstraint("capability_id", name="uq_mcp_tools_capability_id"),
        CheckConstraint(
            "registration_state IN ("
            "'discovered', 'registered', 'disabled', 'revoked', 'schema_changed')",
            name="registration_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    server_id: Mapped[str] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="RESTRICT", name="fk_mcp_tools_server_id")
    )
    tool_name: Mapped[str] = mapped_column(String(128))
    capability_id: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text, default="")
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON)
    risk: Mapped[str | None] = mapped_column(String(32), nullable=True)
    permission_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verification_kind: Mapped[str | None] = mapped_column(String(128), nullable=True)
    registration_state: Mapped[str] = mapped_column(String(32), default="discovered")
    schema_hash: Mapped[str] = mapped_column(String(64), default="")
    version: Mapped[str] = mapped_column(String(32), default="0")
    available: Mapped[bool] = mapped_column(Boolean, default=False)
    reconciles_capability_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    registered_by_principal_id: Mapped[str | None] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_mcp_tools_registered_by"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())


class CapabilityVersion(Base):
    """Immutable schema identity for a registered capability."""

    __tablename__ = "capability_versions"
    __table_args__ = (
        UniqueConstraint(
            "capability_id", "version", name="uq_capability_versions_capability_version"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    capability_id: Mapped[str] = mapped_column(String(256))
    version: Mapped[str] = mapped_column(String(32))
    schema_hash: Mapped[str] = mapped_column(String(64))
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class RegistrationEvent(Base):
    """Audit row for MCP registration, disablement, and revocation."""

    __tablename__ = "registration_events"
    __table_args__ = (Index("ix_registration_events_tool", "mcp_tool_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mcp_tool_id: Mapped[str] = mapped_column(
        ForeignKey("mcp_tools.id", ondelete="RESTRICT", name="fk_registration_events_tool")
    )
    actor_principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_registration_events_actor")
    )
    action: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class ReconciliationAttempt(Base):
    """One independent read of an uncertain external operation."""

    __tablename__ = "reconciliation_attempts"
    __table_args__ = (Index("ix_reconciliation_attempts_execution", "execution_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("executions.id", ondelete="RESTRICT", name="fk_reconciliation_execution")
    )
    actor_principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_reconciliation_actor")
    )
    certainty_before: Mapped[str] = mapped_column(String(32))
    certainty_after: Mapped[str] = mapped_column(String(32))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


_MEMORY_CATEGORIES = (
    "fact",
    "preference",
    "decision",
    "constraint",
    "procedure",
    "observation",
    "event",
    "hypothesis",
    "verified_outcome",
)
_MEMORY_RELIABILITY = (
    "unverified",
    "observed",
    "inferred",
    "verified",
    "disputed",
    "superseded",
)
_MEMORY_LIFECYCLE = ("candidate", "active", "superseded", "expired", "deleted")
_MEMORY_SOURCES = ("user", "tool", "model", "task", "system")


class MemoryRecord(Base):
    """Current governed memory. History lives in revisions, not in a silent overwrite."""

    __tablename__ = "memories"
    __table_args__ = (
        CheckConstraint(_in_clause("scope_type", _SCOPES), name="ck_memories_scope"),
        CheckConstraint(_in_clause("category", _MEMORY_CATEGORIES), name="ck_memories_category"),
        CheckConstraint(
            _in_clause("reliability", _MEMORY_RELIABILITY), name="ck_memories_reliability"
        ),
        CheckConstraint(_in_clause("lifecycle", _MEMORY_LIFECYCLE), name="ck_memories_lifecycle"),
        CheckConstraint(_in_clause("source_type", _MEMORY_SOURCES), name="ck_memories_source"),
        Index("ix_memories_scope_lifecycle", "scope_type", "scope_id", "lifecycle"),
        Index("ix_memories_owner", "owner_principal_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(32))
    scope_id: Mapped[str] = mapped_column(String(128))
    owner_principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_memories_owner")
    )
    category: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    reliability: Mapped[str] = mapped_column(String(32))
    lifecycle: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    supersedes_id: Mapped[str | None] = mapped_column(
        ForeignKey("memories.id", ondelete="RESTRICT", name="fk_memories_supersedes"),
        nullable=True,
    )
    source_type: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(128))
    source_principal_id: Mapped[str | None] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_memories_source_principal"),
        nullable=True,
    )
    content_digest: Mapped[str] = mapped_column(String(64))
    verification_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    effective_at: Mapped[datetime] = mapped_column(UtcDateTime())
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class MemoryRevision(Base):
    """Immutable copy of a memory at one version."""

    __tablename__ = "memory_revisions"
    __table_args__ = (
        UniqueConstraint("memory_id", "version", name="uq_memory_revisions_version"),
        Index("ix_memory_revisions_memory", "memory_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    memory_id: Mapped[str] = mapped_column(
        ForeignKey("memories.id", ondelete="RESTRICT", name="fk_memory_revisions_memory")
    )
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    reliability: Mapped[str] = mapped_column(String(32))
    lifecycle: Mapped[str] = mapped_column(String(32))
    actor_principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_memory_revisions_actor")
    )
    reason: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class MemoryEvent(Base):
    """Audit of a memory mutation. Detail does not include memory content."""

    __tablename__ = "memory_events"
    __table_args__ = (Index("ix_memory_events_memory", "memory_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    memory_id: Mapped[str] = mapped_column(
        ForeignKey("memories.id", ondelete="RESTRICT", name="fk_memory_events_memory")
    )
    event_type: Mapped[str] = mapped_column(String(32))
    actor_principal_id: Mapped[str | None] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_memory_events_actor"),
        nullable=True,
    )
    detail: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


_AGENT_STATUSES = ("draft", "registered", "active", "disabled", "retired")
_GOAL_STATUSES = (
    "submitted",
    "planned",
    "running",
    "awaiting_approval",
    "completed",
    "partial",
    "failed",
    "cancelled",
    "uncertain",
)
_SUBTASK_STATUSES = (
    "pending",
    "ready",
    "running",
    "completed",
    "failed",
    "blocked",
    "cancelled",
    "uncertain",
    "awaiting_approval",
)


class LogicalAgent(Base):
    """A role definition. Status is not a permission grant."""

    __tablename__ = "logical_agents"
    __table_args__ = (
        UniqueConstraint("agent_key", name="uq_logical_agents_key"),
        CheckConstraint(_in_clause("status", _AGENT_STATUSES), name="ck_logical_agents_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_key: Mapped[str] = mapped_column(String(64))
    agent_type: Mapped[str] = mapped_column(String(32))
    display_name: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32))
    allowed_permissions: Mapped[list[str]] = mapped_column(JSON)
    autonomy: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())


class Goal(Base):
    """A coordinated objective. The parent task is attribution, not a second authority."""

    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint(_in_clause("status", _GOAL_STATUSES), name="ck_goals_status"),
        Index("ix_goals_principal", "principal_id"),
        Index("ix_goals_lease", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_goals_principal")
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT", name="fk_goals_task")
    )
    original_request: Mapped[str] = mapped_column(Text)
    objective: Mapped[str] = mapped_column(Text)
    project_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer)
    autonomy: Mapped[str] = mapped_column(String(32))
    allow_partial: Mapped[bool] = mapped_column(Boolean, default=False)
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True, default=None
    )
    recovery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())


class GoalSubtask(Base):
    """One node in a goal graph. Dependencies are other subtask keys."""

    __tablename__ = "goal_subtasks"
    __table_args__ = (
        UniqueConstraint("goal_id", "subtask_key", name="uq_goal_subtasks_key"),
        CheckConstraint(_in_clause("status", _SUBTASK_STATUSES), name="ck_goal_subtasks_status"),
        Index("ix_goal_subtasks_goal", "goal_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    goal_id: Mapped[str] = mapped_column(
        ForeignKey("goals.id", ondelete="RESTRICT", name="fk_goal_subtasks_goal")
    )
    subtask_key: Mapped[str] = mapped_column(String(64))
    agent_key: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    capability_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    permission_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    depends_on: Mapped[list[str]] = mapped_column(JSON)
    acceptance: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32))
    plan_step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())


class AgentInvocation(Base):
    """One structured agent attempt. Output is not authority."""

    __tablename__ = "agent_invocations"
    __table_args__ = (
        UniqueConstraint("subtask_id", "attempt", name="uq_agent_invocations_attempt"),
        Index("ix_agent_invocations_goal", "goal_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    goal_id: Mapped[str] = mapped_column(
        ForeignKey("goals.id", ondelete="RESTRICT", name="fk_agent_invocations_goal")
    )
    subtask_id: Mapped[str] = mapped_column(
        ForeignKey("goal_subtasks.id", ondelete="RESTRICT", name="fk_agent_invocations_subtask")
    )
    agent_key: Mapped[str] = mapped_column(String(64))
    principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_agent_invocations_principal")
    )
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class PlanRevision(Base):
    """A validated graph revision. The previous graph is retained."""

    __tablename__ = "plan_revisions"
    __table_args__ = (
        UniqueConstraint("goal_id", "revision", name="uq_plan_revisions_goal_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    goal_id: Mapped[str] = mapped_column(
        ForeignKey("goals.id", ondelete="RESTRICT", name="fk_plan_revisions_goal")
    )
    revision: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(500))
    trigger: Mapped[str] = mapped_column(String(64))
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class GoalEvent(Base):
    """Coordination audit. Payloads do not include secrets or memory content."""

    __tablename__ = "goal_events"
    __table_args__ = (Index("ix_goal_events_goal", "goal_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    goal_id: Mapped[str] = mapped_column(
        ForeignKey("goals.id", ondelete="RESTRICT", name="fk_goal_events_goal")
    )
    event_type: Mapped[str] = mapped_column(String(64))
    actor_principal_id: Mapped[str | None] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_goal_events_actor"),
        nullable=True,
    )
    agent_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class GoalVerification(Base):
    """A verification decision for one plan revision. A stale revision does not count."""

    __tablename__ = "goal_verifications"
    __table_args__ = (
        Index("ix_goal_verifications_goal", "goal_id", "revision"),
        CheckConstraint(
            "status IN ('passed', 'failed', 'inconclusive', "
            "'partially_verified', 'not_applicable')",
            name="ck_goal_verifications_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    goal_id: Mapped[str] = mapped_column(
        ForeignKey("goals.id", ondelete="RESTRICT", name="fk_goal_verifications_goal")
    )
    subtask_id: Mapped[str] = mapped_column(
        ForeignKey("goal_subtasks.id", ondelete="RESTRICT", name="fk_goal_verifications_subtask")
    )
    revision: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON)
    failure_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verifier: Mapped[str] = mapped_column(String(64))
    shared_model: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class MemoryPurge(Base):
    """Proof a memory row was physically removed. The content is not copied here."""

    __tablename__ = "memory_purges"
    __table_args__ = (Index("ix_memory_purges_scope", "scope_type", "scope_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(36))
    scope_type: Mapped[str] = mapped_column(String(32))
    scope_id: Mapped[str] = mapped_column(String(128))
    revision_count: Mapped[int] = mapped_column(Integer)
    purged_at: Mapped[datetime] = mapped_column(UtcDateTime())


class Conversation(Base):
    """A multimodal session. This is not governed memory."""

    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_principal", "principal_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_conversations_principal")
    )
    project_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())


class InteractionRecord(Base):
    """One turn. Raw media bytes are not stored in this row."""

    __tablename__ = "interactions"
    __table_args__ = (
        UniqueConstraint(
            "principal_id",
            "idempotency_key",
            name="uq_interactions_principal_idempotency",
        ),
        CheckConstraint(
            "status IN ('completed', 'failed')",
            name="ck_interactions_status",
        ),
        Index("ix_interactions_conversation", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="RESTRICT", name="fk_interactions_conversation")
    )
    principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_interactions_principal")
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class MediaObject(Base):
    """Metadata for a temporary file. The bytes live outside PostgreSQL."""

    __tablename__ = "media_objects"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_media_objects_storage_key"),
        CheckConstraint("kind IN ('image', 'audio')", name="ck_media_objects_kind"),
        Index("ix_media_objects_principal", "principal_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_media_objects_principal")
    )
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="RESTRICT", name="fk_media_objects_conversation"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(16))
    media_type: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(32))
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provenance: Mapped[str] = mapped_column(String(32))
    trust: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime())
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())


class Connection(Base):
    """An external system Apex has recorded. Credentials are references only."""

    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint("connection_key", name="uq_connections_connection_key"),
        CheckConstraint(
            "kind IN ('mcp', 'api', 'device')",
            name="ck_connections_kind",
        ),
        CheckConstraint(
            "state IN ("
            "'pending_configuration', 'pending_authorization', 'active', "
            "'degraded', 'disabled', 'revoked', 'failed')",
            name="ck_connections_state",
        ),
        CheckConstraint(
            "trust IN ('untrusted', 'reviewed')",
            name="ck_connections_trust",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    connection_key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    display_name: Mapped[str] = mapped_column(String(256))
    owner_principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_connections_owner")
    )
    project_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_identity: Mapped[str] = mapped_column(String(256), default="")
    endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_method: Mapped[str] = mapped_column(String(32), default="none")
    secret_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    scopes: Mapped[list[str]] = mapped_column(JSON)
    trust: Mapped[str] = mapped_column(String(32), default="untrusted")
    state: Mapped[str] = mapped_column(String(32))
    mcp_server_id: Mapped[str | None] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="RESTRICT", name="fk_connections_mcp_server"),
        nullable=True,
    )
    capability_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    last_health_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())


class ConnectionOperation(Base):
    """A discovered connector operation. It is not executable until Apex accepts it."""

    __tablename__ = "connection_operations"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "operation_key",
            name="uq_connection_operations_connection_operation",
        ),
        UniqueConstraint("capability_id", name="uq_connection_operations_capability"),
        CheckConstraint(
            "registration_state IN ("
            "'discovered', 'registered', 'disabled', 'revoked', 'schema_changed')",
            name="ck_connection_operations_state",
        ),
        CheckConstraint(
            "side_effect IN ('read', 'idempotent_write', 'non_idempotent_write')",
            name="ck_connection_operations_side_effect",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    connection_id: Mapped[str] = mapped_column(
        ForeignKey(
            "connections.id",
            ondelete="RESTRICT",
            name="fk_connection_operations_connection",
        )
    )
    operation_key: Mapped[str] = mapped_column(String(64))
    capability_id: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text, default="")
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSON)
    schema_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(32), default="0")
    risk: Mapped[str | None] = mapped_column(String(32), nullable=True)
    permission_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    side_effect: Mapped[str] = mapped_column(String(32))
    registration_state: Mapped[str] = mapped_column(String(32), default="discovered")
    available: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())


class DeviceRecord(Base):
    """A paired device. Pairing is not a permission and not a principal."""

    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("device_key", name="uq_devices_device_key"),
        UniqueConstraint("connection_id", name="uq_devices_connection"),
        CheckConstraint(
            "pairing_state IN ('unpaired', 'paired', 'revoked')",
            name="ck_devices_pairing_state",
        ),
        CheckConstraint(
            "health IN ('unknown', 'ok', 'unavailable')",
            name="ck_devices_health",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_key: Mapped[str] = mapped_column(String(64))
    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id", ondelete="RESTRICT", name="fk_devices_connection")
    )
    device_type: Mapped[str] = mapped_column(String(64))
    owner_principal_id: Mapped[str] = mapped_column(
        ForeignKey("principals.id", ondelete="RESTRICT", name="fk_devices_owner")
    )
    pairing_state: Mapped[str] = mapped_column(String(32), default="unpaired")
    pairing_code_hash: Mapped[str] = mapped_column(String(64))
    protocol_version: Mapped[str] = mapped_column(String(32), default="1")
    health: Mapped[str] = mapped_column(String(32), default="unknown")
    last_seen_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())
