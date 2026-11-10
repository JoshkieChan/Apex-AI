"""Built-in logical agents. Registration does not grant a permission."""

from __future__ import annotations

from datetime import datetime

BUILT_IN: tuple[tuple[str, str, str, str, tuple[str, ...], str], ...] = (
    (
        "coordinator",
        "coordinator",
        "Coordinator",
        "Track the goal, its dependencies, and the final outcome.",
        (),
        "supervised",
    ),
    (
        "planner",
        "planner",
        "Planner",
        "Propose a bounded graph. Apex validates it.",
        (),
        "advisory",
    ),
    (
        "researcher",
        "researcher",
        "Researcher",
        "Read authorized information and return structured findings.",
        ("text.analyze", "records.lookup", "reference.lookup", "device.read"),
        "supervised",
    ),
    (
        "executor",
        "executor",
        "Executor",
        "Run an authorized capability through the execution engine.",
        (
            "text.analyze",
            "records.lookup",
            "records.update",
            "records.export",
            "reference.lookup",
            "reference.note",
            "device.read",
            "device.configure",
        ),
        "bounded",
    ),
    (
        "verifier",
        "verifier",
        "Verifier",
        "Check results against the original inputs. Do not treat a claim as success.",
        (),
        "advisory",
    ),
)


def agent_rows(now: datetime) -> list[dict[str, object]]:
    """Return insert values for agents that are not already stored."""
    rows: list[dict[str, object]] = []
    for key, agent_type, name, purpose, permissions, autonomy in BUILT_IN:
        rows.append(
            {
                "agent_key": key,
                "agent_type": agent_type,
                "display_name": name,
                "purpose": purpose,
                "status": "active",
                "allowed_permissions": list(permissions),
                "autonomy": autonomy,
                "created_at": now,
                "updated_at": now,
            }
        )
    return rows
