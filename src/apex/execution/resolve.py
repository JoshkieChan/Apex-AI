"""Overlay planner-owned bindings onto a payload. User JSON is never treated as a binding."""

from __future__ import annotations

from typing import Any

from apex.errors import DomainError


def resolve_bindings(
    payload: dict[str, Any],
    bindings: dict[str, Any],
    prior_outputs: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Return a concrete input. Bindings are a separate map, not a `$ref` inside user data."""
    resolved = dict(payload)
    for field, binding in bindings.items():
        if not isinstance(binding, dict) or "step_index" not in binding:
            raise DomainError("unresolved_dependency", "An input binding is malformed.")
        source = binding["step_index"]
        if source not in prior_outputs:
            raise DomainError(
                "unresolved_dependency",
                "An input binding depends on a step that has not completed.",
            )
        resolved[field] = prior_outputs[source]
    return resolved
