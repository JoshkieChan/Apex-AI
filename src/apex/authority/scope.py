"""Resource-scope checks. The planner does not decide whether a resource is allowed."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from apex.errors import DomainError


def scope_allows(parent: str, child: str) -> bool:
    """Return whether every resource named by child is inside parent."""
    parent_resources = _parse(parent)
    child_resources = _parse(child)
    if parent_resources is None:
        return True
    if child_resources is None:
        return False
    return child_resources <= parent_resources


def enforce_resource_scope(
    scope: str,
    payload: Mapping[str, Any],
    resource_fields: tuple[str, ...],
) -> None:
    """Reject a delegated payload that leaves the delegation scope.

    A scope of ``*`` allows every extracted reference. A narrowed scope requires
    every reference, and a capability with no resource field cannot satisfy it.
    """
    allowed = _parse(scope)
    if allowed is None and not resource_fields:
        return
    if not resource_fields:
        raise DomainError(
            "scope_violation",
            "The delegated capability has no resource reference for this scope.",
        )
    refs = _references(payload, resource_fields)
    if allowed is None:
        return
    if any(ref not in allowed for ref in refs):
        raise DomainError(
            "scope_violation",
            "The operation refers to a resource outside the delegated scope.",
        )


def _parse(scope: str) -> set[str] | None:
    text = scope.strip()
    if text == "*":
        return None
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts or any(part == "*" for part in parts):
        raise DomainError("scope_violation", "The resource scope is not valid.")
    return set(parts)


def _references(payload: Mapping[str, Any], fields: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for field in fields:
        if field not in payload:
            raise DomainError(
                "scope_violation",
                "The operation is missing a required resource reference.",
            )
        found.extend(_values(payload[field]))
    if not found:
        raise DomainError(
            "scope_violation",
            "The operation is missing a required resource reference.",
        )
    return found


def _values(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, str) and item.strip() for item in value)
    ):
        return [item.strip() for item in value]
    raise DomainError(
        "scope_violation",
        "The operation resource reference is not valid.",
    )
