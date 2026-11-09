"""Build a plan from a deterministic intent and the capability registry."""

from __future__ import annotations

from apex.capabilities.registry import CapabilityRegistry
from apex.contracts.schemas import Intent, PlanDraft, PlanStepDraft
from apex.errors import DomainError
from apex.planning.validation import validate_plan


class PlanningEngine:
    """Create an ordered plan. Model-suggested capabilities are ignored by the caller."""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    def build(self, intent: Intent) -> PlanDraft:
        if not intent.required_capabilities:
            raise DomainError(
                "invalid_plan", "The request does not map to a registered capability."
            )
        steps: list[PlanStepDraft] = []
        for index, capability_id in enumerate(intent.required_capabilities):
            spec = self._registry.get(capability_id)
            if spec is None or not spec.available:
                raise DomainError(
                    "unregistered_capability",
                    "The plan references a capability that is not registered.",
                )
            depends = [index - 1] if index > 0 else []
            steps.append(
                PlanStepDraft(
                    step_index=index,
                    capability_id=spec.id,
                    objective=spec.description,
                    input_payload=intent.inputs.get(spec.id, {}),
                    input_bindings=intent.bindings.get(spec.id, {}),
                    depends_on=depends,
                    permission_key=spec.permission_key,
                    risk=spec.risk,
                    verification_kind=spec.verification_kind,
                    capability_version=spec.version,
                    schema_hash=spec.schema_hash,
                    failure_policy={
                        "max_attempts": spec.max_attempts,
                        "on_failure": "fail_task",
                    },
                )
            )
        draft = PlanDraft(objective=intent.objective, steps=steps)
        validate_plan(draft, self._registry)
        return draft
