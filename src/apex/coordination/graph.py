"""Deterministic goal graphs. A model cannot submit an unvalidated graph."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from apex.capabilities.registry import CapabilityRegistry
from apex.errors import DomainError

# A role allowlist is an upper bound. It is not a permission grant.
HARD_PERMISSIONS: dict[str, frozenset[str]] = {
    "coordinator": frozenset(),
    "planner": frozenset(),
    "researcher": frozenset({"text.analyze", "records.lookup", "reference.lookup", "device.read"}),
    "executor": frozenset(
        {
            "text.analyze",
            "records.lookup",
            "records.update",
            "records.export",
            "reference.lookup",
            "reference.note",
            "device.read",
            "device.configure",
        }
    ),
    "verifier": frozenset(),
}

_PARALLEL = re.compile(
    r"^Coordinate: count the words in '(?P<a>[^']+)' and count the words in '(?P<b>[^']+)'$"
)
_SEQUENTIAL = re.compile(
    r"^Coordinate sequentially: count the words in '(?P<a>[^']+)' "
    r"then count the words in '(?P<b>[^']+)'$"
)
_FAIL = re.compile(r"^Coordinate: count the words in '(?P<a>[^']+)' then fail the next agent$")
_FORBIDDEN = re.compile(
    r"^Coordinate a forbidden export to (?P<label>[A-Za-z0-9][A-Za-z0-9_-]{0,63})$"
)
_EXPORT = re.compile(r"^Coordinate an export to (?P<label>[A-Za-z0-9][A-Za-z0-9_-]{0,63})$")
_SYSTEMS = re.compile(
    r"^Coordinate reference systems: look up '(?P<item>[A-Za-z0-9][A-Za-z0-9_-]{0,63})' "
    r"and label the reference device '(?P<label>[A-Za-z0-9][A-Za-z0-9_-]{0,63})'$"
)


@dataclass(frozen=True)
class AgentRule:
    """The registry facts the graph checker is allowed to see."""

    agent_key: str
    agent_type: str
    status: str
    allowed_permissions: frozenset[str]


@dataclass(frozen=True)
class GraphNode:
    """One proposed subtask. Capability permission comes from the registry."""

    key: str
    agent_key: str
    description: str
    capability_id: str | None
    input_payload: dict[str, Any]
    depends_on: tuple[str, ...]
    acceptance: dict[str, Any]
    fail: bool = False


def decompose(objective: str) -> tuple[list[GraphNode], bool]:
    """Map a known objective onto a small graph. Unknown text is not a goal."""
    parallel = _PARALLEL.match(objective)
    if parallel:
        return _counts(parallel.group("a"), parallel.group("b"), sequential=False), False
    sequential = _SEQUENTIAL.match(objective)
    if sequential:
        return _counts(sequential.group("a"), sequential.group("b"), sequential=True), False
    failed = _FAIL.match(objective)
    if failed:
        first = _analyze("s0", "executor", failed.group("a"), ())
        crash = GraphNode(
            key="s1",
            agent_key="executor",
            description="Simulated agent failure.",
            capability_id=None,
            input_payload={"fail": True},
            depends_on=("s0",),
            acceptance={"kind": "agent_result"},
            fail=True,
        )
        check = _verifier("s2", ("s0", "s1"))
        return [first, crash, check], True
    forbidden = _FORBIDDEN.match(objective)
    if forbidden:
        return [_export("researcher", forbidden.group("label"))], False
    export = _EXPORT.match(objective)
    if export:
        return [_export("executor", export.group("label"))], False
    systems = _SYSTEMS.match(objective)
    if systems:
        return _reference_systems(systems.group("item"), systems.group("label")), False
    raise DomainError("not_a_goal", "The objective is not a coordinated goal.")


def validate_graph(
    nodes: list[GraphNode],
    *,
    agents: dict[str, AgentRule],
    registry: CapabilityRegistry,
    limit: int,
) -> None:
    """Reject a graph before any capability runs."""
    if not nodes or len(nodes) > limit:
        raise DomainError("plan_invalid", "The plan exceeds the subtask limit.")
    keys = [node.key for node in nodes]
    if len(keys) != len(set(keys)):
        raise DomainError("plan_invalid", "Subtask identifiers must be unique.")
    known = set(keys)
    for node in nodes:
        if not node.acceptance:
            raise DomainError("plan_invalid", "Each subtask needs acceptance criteria.")
        if node.key in node.depends_on:
            raise DomainError("plan_invalid", "A subtask cannot depend on itself.")
        if any(dep not in known for dep in node.depends_on):
            raise DomainError("plan_invalid", "A dependency does not exist.")
        agent = agents.get(node.agent_key)
        if agent is None or agent.status != "active":
            raise DomainError("agent_unavailable", "The assigned agent is not active.")
        _check_assignment(node, agent, registry)
    _reject_cycles(nodes)


def _check_assignment(node: GraphNode, agent: AgentRule, registry: CapabilityRegistry) -> None:
    if node.fail:
        if node.capability_id is not None:
            raise DomainError("plan_invalid", "A failure marker cannot name a capability.")
        return
    if node.capability_id is None:
        if agent.agent_type != "verifier":
            raise DomainError("plan_invalid", "Only a verifier can run without a capability.")
        return
    spec = registry.get(node.capability_id)
    if spec is None or not spec.available:
        raise DomainError("plan_invalid", "The plan names a capability that is not available.")
    allowed = HARD_PERMISSIONS.get(agent.agent_type, frozenset()) & agent.allowed_permissions
    if spec.permission_key not in allowed:
        raise DomainError("role_forbidden", "The assigned agent cannot use that capability.")


def _reject_cycles(nodes: list[GraphNode]) -> None:
    graph = {node.key: node.depends_on for node in nodes}
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(key: str) -> None:
        if key in visiting:
            raise DomainError("plan_invalid", "The plan contains a dependency cycle.")
        if key in visited:
            return
        visiting.add(key)
        for dependency in graph[key]:
            walk(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in graph:
        walk(key)


def _counts(first: str, second: str, *, sequential: bool) -> list[GraphNode]:
    agent = "executor" if sequential else "researcher"
    opening = _analyze("s0", agent, first, ())
    follower = _analyze("s1", agent, second, ("s0",) if sequential else ())
    return [opening, follower, _verifier("s2", ("s0", "s1"))]


def _analyze(key: str, agent: str, text: str, depends_on: tuple[str, ...]) -> GraphNode:
    return GraphNode(
        key=key,
        agent_key=agent,
        description=f"Count the words in the assigned text ({key}).",
        capability_id="text.analyze",
        input_payload={"text": text},
        depends_on=depends_on,
        acceptance={"kind": "word_count"},
    )


def _verifier(key: str, sources: tuple[str, ...]) -> GraphNode:
    return GraphNode(
        key=key,
        agent_key="verifier",
        description="Recompute the word counts from the original text.",
        capability_id=None,
        input_payload={},
        depends_on=sources,
        acceptance={"kind": "word_counts", "sources": list(sources)},
    )


def _reference_systems(item: str, label: str) -> list[GraphNode]:
    lookup = GraphNode(
        key="s0",
        agent_key="executor",
        description="Read the reference inventory item.",
        capability_id="api.reference.lookup",
        input_payload={"item": item},
        depends_on=(),
        acceptance={"kind": "reference.lookup"},
    )
    configure = GraphNode(
        key="s1",
        agent_key="executor",
        description="Set the reference device label.",
        capability_id="device.reference-device.configure",
        input_payload={"device_key": "reference-device", "label": label},
        depends_on=("s0",),
        acceptance={"kind": "device.configure"},
    )
    check = _verifier("s2", ("s0", "s1"))
    return [lookup, configure, check]


def _export(agent: str, label: str) -> GraphNode:
    return GraphNode(
        key="s0",
        agent_key=agent,
        description="Export a record through the assigned agent.",
        capability_id="records.export",
        input_payload={"destination_label": label, "content": {"note": "goal"}},
        depends_on=(),
        acceptance={"kind": "export_receipt"},
    )
