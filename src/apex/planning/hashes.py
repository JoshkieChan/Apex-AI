"""Operation hashes for plan steps. Approval checks recompute these from stored inputs."""

from __future__ import annotations

import hashlib
from typing import Any

from apex.runtime_ids import hash_operation


def hashes_for_steps(
    steps: list[Any],
    *,
    principal_id: str = "",
    delegation_id: str | None = None,
    resource_scope: str = "",
) -> dict[int, str]:
    """Hash steps in index order. Each hash includes the hashes of its dependencies."""
    ordered = sorted(steps, key=lambda step: step.step_index)
    hashes: dict[int, str] = {}
    for step in ordered:
        depends = [int(index) for index in step.depends_on]
        upstream = [hashes[index] for index in depends]
        hashes[int(step.step_index)] = hash_operation(
            step.capability_id,
            dict(step.input_payload),
            dict(step.input_bindings or {}),
            upstream,
            capability_version=str(getattr(step, "capability_version", "1") or "1"),
            schema_hash=str(getattr(step, "schema_hash", "") or ""),
            principal_id=principal_id,
            delegation_id=delegation_id or "",
            resource_scope=resource_scope,
        )
    return hashes


def plan_hash(step_hashes: dict[int, str]) -> str:
    """Hash the ordered step hashes so a plan version has a stable identity."""
    joined = "|".join(step_hashes[index] for index in sorted(step_hashes))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
