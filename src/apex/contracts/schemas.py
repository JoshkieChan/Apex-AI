"""Pydantic contracts shared by the API, runtime, and model adapters."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskStatus(StrEnum):
    submitted = "submitted"
    interpreting = "interpreting"
    planned = "planned"
    awaiting_approval = "awaiting_approval"
    executing = "executing"
    verifying = "verifying"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    uncertain = "uncertain"


TERMINAL_TASK_STATUSES = frozenset({TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled})
# Uncertain work is not retried by the worker. Reconciliation is the only exit.
IDLE_TASK_STATUSES = TERMINAL_TASK_STATUSES | {TaskStatus.uncertain}

LEGAL_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.submitted: frozenset(
        {TaskStatus.interpreting, TaskStatus.failed, TaskStatus.cancelled}
    ),
    TaskStatus.interpreting: frozenset(
        {TaskStatus.planned, TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled}
    ),
    TaskStatus.planned: frozenset(
        {
            TaskStatus.awaiting_approval,
            TaskStatus.executing,
            TaskStatus.failed,
            TaskStatus.cancelled,
        }
    ),
    TaskStatus.awaiting_approval: frozenset(
        {TaskStatus.executing, TaskStatus.failed, TaskStatus.cancelled}
    ),
    TaskStatus.executing: frozenset(
        {
            TaskStatus.awaiting_approval,
            TaskStatus.verifying,
            TaskStatus.failed,
            TaskStatus.cancelled,
            TaskStatus.uncertain,
        }
    ),
    TaskStatus.verifying: frozenset(
        {TaskStatus.completed, TaskStatus.failed, TaskStatus.uncertain}
    ),
    TaskStatus.completed: frozenset(),
    TaskStatus.failed: frozenset(),
    TaskStatus.cancelled: frozenset(),
    TaskStatus.uncertain: frozenset({TaskStatus.completed, TaskStatus.failed}),
}

CANCELLABLE_TASK_STATUSES = frozenset(
    {
        TaskStatus.submitted,
        TaskStatus.interpreting,
        TaskStatus.planned,
        TaskStatus.awaiting_approval,
    }
)


class Decision(StrEnum):
    allow = "allow"
    deny = "deny"
    require_approval = "require_approval"


class ApprovalStatus(StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"
    cancelled = "cancelled"


class ExecutionStatus(StrEnum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    timed_out = "timed_out"
    uncertain = "uncertain"


class VerificationStatus(StrEnum):
    passed = "passed"
    failed = "failed"
    inconclusive = "inconclusive"


class RiskLevel(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    unknown = "unknown"


class ContextScope(StrEnum):
    personal = "personal"
    project = "project"
    task = "task"
    system = "system"


class TrustLevel(StrEnum):
    proposal = "proposal"
    trusted = "trusted"


class OutcomeKind(StrEnum):
    executed = "executed"
    clarification = "clarification"
    denied = "denied"
    rejected = "rejected"
    failed = "failed"
    uncertain = "uncertain"


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskCreateRequest(ModelConfig):
    request: str = Field(min_length=1, max_length=8000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    project_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    delegation_id: str | None = Field(default=None, min_length=36, max_length=36)


class ApprovalDecisionRequest(ModelConfig):
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=1000)


class Intent(ModelConfig):
    objective: str
    required_capabilities: list[str]
    constraints: list[str] = Field(default_factory=list)
    risk: RiskLevel = RiskLevel.unknown
    expected_outcome: str
    missing_information: list[str] = Field(default_factory=list)
    clarification_required: bool = False
    sensitivity_flags: list[str] = Field(default_factory=list)
    inputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    bindings: dict[str, dict[str, dict[str, int]]] = Field(default_factory=dict)
    source: Literal["deterministic", "deterministic+model"] = "deterministic"
    dropped_model_capabilities: list[str] = Field(default_factory=list)
    model_objective: str | None = None


class IntentDraft(ModelConfig):
    """Model-produced interpretation. It cannot carry an authorization decision."""

    objective: str
    required_capabilities: list[str]
    constraints: list[str] = Field(default_factory=list)
    risk: RiskLevel = RiskLevel.unknown
    expected_outcome: str
    missing_information: list[str] = Field(default_factory=list)
    clarification_required: bool = False
    sensitivity_flags: list[str] = Field(default_factory=list)


class CoordinationNote(ModelConfig):
    summary: str
    suggested_capabilities: list[str] = Field(default_factory=list)


class VerificationNote(ModelConfig):
    note: str
    suggested_status: VerificationStatus


class PlanStepDraft(ModelConfig):
    step_index: int = Field(ge=0)
    capability_id: str
    objective: str
    input_payload: dict[str, Any]
    input_bindings: dict[str, dict[str, int]] = Field(default_factory=dict)
    depends_on: list[int] = Field(default_factory=list)
    permission_key: str
    risk: RiskLevel
    verification_kind: str
    failure_policy: dict[str, Any]
    capability_version: str = "1"
    schema_hash: str = ""


class PlanDraft(ModelConfig):
    objective: str
    steps: list[PlanStepDraft]


class CheckResult(ModelConfig):
    name: str
    passed: bool
    detail: str


class VerificationReport(ModelConfig):
    status: VerificationStatus
    checks: list[CheckResult]
    summary: str
    advisory_note: str | None = None
    advisory_suggestion: VerificationStatus | None = None
    advisory_disagrees: bool = False


class TokenUsage(ModelConfig):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class ModelMessage(ModelConfig):
    role: Literal["system", "user", "assistant"]
    content: str


class ModelRequest(ModelConfig):
    model: str
    messages: list[ModelMessage]
    schema_name: str
    json_schema: dict[str, Any]
    timeout_seconds: float = 30.0
    temperature: float = 0.0
    memory_context: dict[str, Any] | None = None


class ModelResponse(ModelConfig):
    provider: str
    model: str
    content: dict[str, Any]
    usage: TokenUsage
    latency_ms: int = 0


class PlanStepView(ModelConfig):
    id: str
    step_index: int
    capability_id: str
    status: str
    permission_key: str
    risk: RiskLevel
    depends_on: list[int]
    input_payload: dict[str, Any]
    input_bindings: dict[str, Any]
    operation_hash: str
    verification_kind: str


class PlanView(ModelConfig):
    id: str
    version: int
    objective: str
    plan_hash: str
    steps: list[PlanStepView]


class ApprovalView(ModelConfig):
    id: str
    task_id: str
    plan_step_id: str
    capability_id: str
    status: ApprovalStatus
    operation_hash: str
    canonical_payload: dict[str, Any]
    canonical_bindings: dict[str, Any]
    requested_by_principal_id: str
    decided_by_principal_id: str | None
    reason: str | None
    expires_at: datetime
    decided_at: datetime | None
    created_at: datetime


class ExecutionView(ModelConfig):
    id: str
    plan_step_id: str
    capability_id: str
    attempt: int
    status: ExecutionStatus
    operation_hash: str
    input_payload: dict[str, Any]
    output_payload: dict[str, Any] | None
    evidence: dict[str, Any]
    error_code: str | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None
    outcome_certainty: str = "not_sent"


class VerificationView(ModelConfig):
    id: str
    execution_id: str
    status: VerificationStatus
    checks: list[CheckResult]
    summary: str
    advisory_note: str | None
    advisory_suggestion: str | None
    advisory_disagrees: bool


class TaskEventView(ModelConfig):
    id: str
    event_type: str
    from_status: str | None
    to_status: str | None
    payload: dict[str, Any]
    created_at: datetime


class TaskErrorView(ModelConfig):
    code: str
    message: str


class TaskView(ModelConfig):
    id: str
    status: TaskStatus
    request_id: str
    request: str
    objective: str | None
    outcome_kind: OutcomeKind | None
    result: dict[str, Any] | None
    error: TaskErrorView | None
    plan: PlanView | None
    approvals: list[ApprovalView]
    executions: list[ExecutionView]
    verifications: list[VerificationView]
    events: list[TaskEventView]
    created_at: datetime
    updated_at: datetime


class TaskStatusView(ModelConfig):
    id: str
    status: TaskStatus
    outcome_kind: OutcomeKind | None
    updated_at: datetime
    error: TaskErrorView | None


class HealthResponse(ModelConfig):
    status: Literal["ok"]
    database: Literal["ok", "unavailable"]
    environment: str
    auth_mode: Literal["development", "bearer", "unconfigured"]
    version: str
    schema_revision: str = ""
    secret_provider: str = ""
    recoverable_goals: int = 0
    leased_goals: int = 0
    uncertain_operations: int = 0
    awaiting_approval: int = 0
    execution_suspended: bool = False
