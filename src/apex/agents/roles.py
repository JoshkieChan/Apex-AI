"""Coordinator, planner, specialist, and verifier roles.

The planner role delegates to the deterministic planning engine. The specialist
can only call the execution engine. The verifier cannot override a failed check.
"""

from __future__ import annotations

from typing import Any

from apex.capabilities.registry import CapabilityRegistry
from apex.contracts.schemas import (
    CoordinationNote,
    Intent,
    ModelMessage,
    ModelRequest,
    PlanDraft,
    VerificationNote,
    VerificationReport,
)
from apex.execution.engine import ExecutionEngine
from apex.logging_config import get_logger
from apex.memory.contracts import AssembledContext
from apex.persistence.models import Approval, Execution, PlanStep
from apex.planning.engine import PlanningEngine
from apex.providers.base import ModelProvider
from apex.providers.context import with_memory
from apex.redaction import redact, safe_identifier
from apex.verification.engine import verify_execution

logger = get_logger("agents")


class Coordinator:
    """Ask the model for a routing note. Suggestions do not select capabilities."""

    def __init__(self, provider: ModelProvider | None, registry: CapabilityRegistry) -> None:
        self._provider = provider
        self._registry = registry

    def advise(
        self,
        text: str,
        accepted: set[str],
        *,
        memory: AssembledContext | None = None,
    ) -> tuple[str | None, list[str]]:
        if self._provider is None:
            return None, []
        request = with_memory(
            ModelRequest(
                model=self._provider.model_name,
                messages=[
                    ModelMessage(
                        role="system",
                        content="Summarize how to route this request. You cannot grant authority.",
                    ),
                    ModelMessage(role="user", content=text),
                ],
                schema_name="CoordinationNote",
                json_schema=CoordinationNote.model_json_schema(),
            ),
            memory,
        )
        try:
            response = self._provider.complete_structured(request)
            note = CoordinationNote.model_validate(response.content)
        except Exception as exc:
            logger.info("coordinator advisory unavailable: %s", type(exc).__name__)
            return None, []
        ignored = [
            safe_identifier(capability_id)
            for capability_id in note.suggested_capabilities
            if capability_id not in accepted or self._registry.get(capability_id) is None
        ]
        return redact(note.summary)[:500], ignored


class Planner:
    """Build a validated plan. This role does not evaluate or grant permission."""

    def __init__(self, engine: PlanningEngine) -> None:
        self._engine = engine

    def create_plan(self, intent: Intent) -> PlanDraft:
        return self._engine.build(intent)


class Specialist:
    """Perform one attributed operation through the execution boundary."""

    def __init__(self, engine: ExecutionEngine) -> None:
        self._engine = engine

    def perform(
        self,
        *,
        task_id: str,
        principal_id: str,
        step: PlanStep,
        approval: Approval | None,
        prior_outputs: dict[int, dict[str, Any]],
        operation_hash: str,
        delegation_id: str | None = None,
    ) -> Execution:
        return self._engine.execute(
            task_id=task_id,
            principal_id=principal_id,
            step=step,
            approval=approval,
            prior_outputs=prior_outputs,
            operation_hash=operation_hash,
            delegation_id=delegation_id,
        )


class Verifier:
    """Review an execution. A model suggestion cannot change a deterministic status."""

    def __init__(self, provider: ModelProvider | None) -> None:
        self._provider = provider

    def review(
        self,
        *,
        verification_kind: str,
        execution_status: str,
        resolved_input: dict[str, Any] | None,
        output: dict[str, Any] | None,
    ) -> VerificationReport:
        report = verify_execution(
            verification_kind=verification_kind,
            execution_status=execution_status,
            resolved_input=resolved_input,
            output=output,
        )
        note, suggestion = self._advisory()
        disagrees = suggestion is not None and suggestion != report.status
        return report.model_copy(
            update={
                "advisory_note": note,
                "advisory_suggestion": suggestion,
                "advisory_disagrees": disagrees,
            }
        )

    def _advisory(self) -> tuple[str | None, Any]:
        if self._provider is None:
            return None, None
        request = ModelRequest(
            model=self._provider.model_name,
            messages=[
                ModelMessage(
                    role="system",
                    content="Provide an advisory note. You cannot override deterministic checks.",
                ),
                ModelMessage(role="user", content="Review the execution using the schema only."),
            ],
            schema_name="VerificationNote",
            json_schema=VerificationNote.model_json_schema(),
        )
        try:
            response = self._provider.complete_structured(request)
            parsed = VerificationNote.model_validate(response.content)
        except Exception as exc:
            logger.info("verifier advisory unavailable: %s", type(exc).__name__)
            return None, None
        return redact(parsed.note)[:500], parsed.suggested_status
