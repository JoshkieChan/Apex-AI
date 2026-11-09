"""Reject malformed plans and impossible dependencies before execution."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from apex.capabilities.registry import CapabilityRegistry
from apex.contracts.schemas import PlanDraft
from apex.errors import DomainError


def validate_plan(draft: PlanDraft, registry: CapabilityRegistry) -> None:
    """Validate indexes, dependencies, registered capabilities, and inputs."""
    if not draft.steps:
        raise DomainError("invalid_plan", "A plan must contain at least one step.")
    indexes = [step.step_index for step in draft.steps]
    if indexes != list(range(len(draft.steps))):
        raise DomainError("invalid_plan", "Plan step indexes must be contiguous and start at 0.")
    for step in draft.steps:
        spec = registry.get(step.capability_id)
        if spec is None or not spec.available:
            raise DomainError(
                "unregistered_capability",
                "The plan references a capability that is not registered.",
            )
        if (
            step.permission_key != spec.permission_key
            or step.verification_kind != spec.verification_kind
            or (step.capability_version and step.capability_version != spec.version)
            or (step.schema_hash and step.schema_hash != spec.schema_hash)
        ):
            raise DomainError(
                "invalid_plan", "Plan permission metadata does not match the registry."
            )
        for dependency in step.depends_on:
            if dependency < 0 or dependency >= step.step_index:
                raise DomainError(
                    "invalid_plan",
                    "A step dependency must reference an earlier step.",
                )
        if len(step.depends_on) != len(set(step.depends_on)):
            raise DomainError("invalid_plan", "A step dependency is duplicated.")
        _validate_bindings(step.input_bindings, step.step_index, step.depends_on)
        _validate_inputs(spec.input_model, step.input_payload, step.input_bindings)


def _validate_bindings(
    bindings: dict[str, dict[str, int]],
    step_index: int,
    depends_on: list[int],
) -> None:
    for field, binding in bindings.items():
        if set(binding.keys()) != {"step_index"}:
            raise DomainError("invalid_plan", f"Input binding for '{field}' is malformed.")
        source = binding["step_index"]
        if source < 0 or source >= step_index or source not in depends_on:
            raise DomainError(
                "invalid_plan",
                "An input binding must reference a declared earlier dependency.",
            )


def _validate_inputs(
    model: type[Any], payload: dict[str, Any], bindings: dict[str, dict[str, int]]
) -> None:
    fields = model.model_fields
    unknown_bindings = [name for name in bindings if name not in fields]
    if unknown_bindings:
        raise DomainError("invalid_plan", "An input binding references an unknown field.")
    if not bindings:
        try:
            model.model_validate(payload)
        except ValidationError as exc:
            raise DomainError(
                "invalid_plan", "A plan step input does not match its schema."
            ) from exc
        return
    trial = dict(payload)
    for name in bindings:
        trial[name] = _placeholder(fields[name].annotation)
    try:
        model.model_validate(trial)
    except ValidationError as exc:
        raise DomainError("invalid_plan", "A plan step input does not match its schema.") from exc


def _placeholder(annotation: Any) -> Any:
    origin = getattr(annotation, "__origin__", None)
    if annotation is str:
        return "placeholder"
    if annotation is int:
        return 0
    if origin is dict or annotation is dict:
        return {"placeholder": True}
    return "placeholder"
