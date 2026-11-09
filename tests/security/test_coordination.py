"""Coordinated goals: responsibilities, authority, and recovery."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.conftest import auth

from apex.capabilities.builtins import build_default_registry
from apex.coordination.graph import AgentRule, GraphNode, validate_graph
from apex.errors import DomainError
from apex.persistence.models import GoalSubtask, LogicalAgent

PARALLEL = "Coordinate: count the words in 'alpha beta' and count the words in 'gamma'"
SEQUENTIAL = (
    "Coordinate sequentially: count the words in 'alpha beta' then count the words in 'gamma'"
)
FAIL = "Coordinate: count the words in 'alpha beta' then fail the next agent"
FORBIDDEN = "Coordinate a forbidden export to finance-archive"
EXPORT = "Coordinate an export to finance-archive"


def test_parallel_agents_keep_their_responsibilities(client: TestClient) -> None:
    body = _goal(client, PARALLEL)
    assert body["status"] == "completed"
    assert body["original_request"] == PARALLEL
    assert body["task_status"] == "completed"
    by_key = {row["key"]: row for row in body["subtasks"]}
    assert by_key["s0"]["agent_key"] == "researcher"
    assert by_key["s1"]["agent_key"] == "researcher"
    assert by_key["s0"]["depends_on"] == []
    assert by_key["s1"]["depends_on"] == []
    assert by_key["s2"]["agent_key"] == "verifier"
    assert by_key["s2"]["depends_on"] == ["s0", "s1"]
    assert by_key["s0"]["output"]["word_count"] == 2
    assert by_key["s1"]["output"]["word_count"] == 1
    assert body["result"]["verification"]["status"] == "passed"
    assert body["result"]["verification"]["shared_model"] is False
    subjects = {row["principal_subject"] for row in body["invocations"]}
    agents = {row["agent_key"] for row in body["invocations"]}
    assert subjects == {"dev-user"}
    assert agents == {"researcher", "verifier"}
    assert "goal_completed" in body["events"]
    assert "verification_passed" in body["events"]


def test_sequential_work_survives_a_restart(client: TestClient) -> None:
    started = _goal(client, SEQUENTIAL, max_waves=1)
    by_key = {row["key"]: row for row in started["subtasks"]}
    assert started["status"] == "running"
    assert by_key["s0"]["status"] == "completed"
    assert by_key["s1"]["status"] == "pending"
    assert by_key["s0"]["attempt_count"] == 1
    finished = client.post(f"/v1/goals/{started['id']}/run", headers=auth()).json()
    again = {row["key"]: row for row in finished["subtasks"]}
    assert finished["status"] == "completed"
    assert again["s0"]["attempt_count"] == 1
    assert again["s0"]["execution_id"] == by_key["s0"]["execution_id"]
    assert again["s1"]["status"] == "completed"
    assert again["s1"]["depends_on"] == ["s0"]
    assert again["s2"]["status"] == "completed"


def test_coordinator_cannot_give_a_researcher_a_write(client: TestClient) -> None:
    response = client.post("/v1/goals", headers=auth(), json={"objective": FORBIDDEN})
    assert response.status_code == 403
    assert response.json()["error"] == "role_forbidden"
    assert client.get("/v1/goals/missing", headers=auth()).status_code == 404


def test_widened_allowlist_does_not_grant_the_write(client: TestClient, session: Session) -> None:
    client.get("/v1/goals/agents", headers=auth())
    agent = session.scalar(select(LogicalAgent).where(LogicalAgent.agent_key == "researcher"))
    assert agent is not None
    agent.allowed_permissions = ["text.analyze", "records.lookup", "records.export"]
    session.commit()
    response = client.post("/v1/goals", headers=auth(), json={"objective": FORBIDDEN})
    assert response.status_code == 403
    assert response.json()["error"] == "role_forbidden"


def test_executor_export_stops_for_approval(client: TestClient) -> None:
    body = _goal(client, EXPORT)
    assert body["status"] == "awaiting_approval"
    assert body["task_status"] == "awaiting_approval"
    subtask = body["subtasks"][0]
    assert subtask["agent_key"] == "executor"
    assert subtask["capability_id"] == "records.export"
    assert subtask["execution_id"] is None
    assert subtask["error_code"] == "approval_required"
    again = client.post(f"/v1/goals/{body['id']}/run", headers=auth())
    assert again.status_code == 200
    assert again.json()["status"] == "awaiting_approval"
    assert again.json()["subtasks"][0]["execution_id"] is None


def test_restricted_principal_cannot_execute(client: TestClient) -> None:
    body = _goal(client, PARALLEL, principal="dev-restricted")
    assert body["status"] == "failed"
    assert body["error_code"] == "permission_denied"
    assert all(row["execution_id"] is None for row in body["subtasks"])
    denied = [row for row in body["subtasks"] if row["error_code"] == "permission_denied"]
    assert len(denied) == 2


def test_disabled_agent_rejects_new_work(client: TestClient) -> None:
    denied = client.post(
        "/v1/goals/agents/researcher",
        headers=auth(),
        json={"status": "disabled"},
    )
    assert denied.status_code == 403
    changed = client.post(
        "/v1/goals/agents/researcher",
        headers=auth("dev-registrar"),
        json={"status": "disabled"},
    )
    assert changed.status_code == 200
    response = client.post("/v1/goals", headers=auth(), json={"objective": PARALLEL})
    assert response.status_code == 400
    assert response.json()["error"] == "agent_unavailable"


def test_failure_replans_without_repeating_completed_work(client: TestClient) -> None:
    failed = _goal(client, FAIL)
    by_key = {row["key"]: row for row in failed["subtasks"]}
    assert failed["status"] == "failed"
    assert by_key["s0"]["status"] == "completed"
    assert by_key["s1"]["status"] == "failed"
    assert by_key["s1"]["execution_id"] is None
    assert by_key["s2"]["status"] == "blocked"
    revised = client.post(f"/v1/goals/{failed['id']}/replan", headers=auth())
    assert revised.status_code == 200
    body = revised.json()
    again = {row["key"]: row for row in body["subtasks"]}
    assert body["status"] == "partial"
    assert body["revision"] == 2
    assert again["s0"]["attempt_count"] == 1
    assert again["s0"]["execution_id"] == by_key["s0"]["execution_id"]
    assert again["s1"]["status"] == "cancelled"
    assert again["s2"]["status"] == "completed"
    assert body["result"]["verification"]["status"] == "passed"
    assert body["task_status"] == "failed"


def test_uncertain_work_is_not_retried(client: TestClient, session: Session) -> None:
    started = _goal(client, SEQUENTIAL, max_waves=1)
    waiting = session.scalar(
        select(GoalSubtask).where(
            GoalSubtask.goal_id == started["id"],
            GoalSubtask.subtask_key == "s1",
        )
    )
    assert waiting is not None
    waiting.status = "uncertain"
    waiting.error_code = "outcome_uncertain"
    session.commit()
    refused = client.post(f"/v1/goals/{started['id']}/replan", headers=auth())
    assert refused.status_code == 409
    assert refused.json()["error"] == "outcome_uncertain"
    continued = client.post(f"/v1/goals/{started['id']}/run", headers=auth()).json()
    by_key = {row["key"]: row for row in continued["subtasks"]}
    assert continued["status"] == "uncertain"
    assert by_key["s0"]["attempt_count"] == 1
    assert by_key["s1"]["status"] == "uncertain"
    assert by_key["s1"]["execution_id"] is None
    assert by_key["s2"]["status"] == "uncertain"


def test_cancel_stops_pending_work(client: TestClient) -> None:
    started = _goal(client, SEQUENTIAL, max_waves=0)
    assert started["status"] == "running"
    cancelled = client.post(f"/v1/goals/{started['id']}/cancel", headers=auth()).json()
    assert cancelled["status"] == "cancelled"
    assert cancelled["task_status"] == "cancelled"
    assert all(row["execution_id"] is None for row in cancelled["subtasks"])
    assert {row["status"] for row in cancelled["subtasks"]} == {"cancelled"}


def test_deleted_and_foreign_memory_are_not_shared(client: TestClient) -> None:
    live = _memory(client, "alpha", "The live project note is BLUE-RIVER")
    deleted = _memory(client, "alpha", "The deleted archive code is PURPLE-MANGO-91")
    other = _memory(client, "beta", "Beta private note ORANGE-QUILL")
    removed = client.delete(f"/v1/memory/{deleted['id']}", headers=auth())
    assert removed.status_code == 200
    body = _goal(client, PARALLEL, project_key="alpha")
    listed: list[str] = []
    for invocation in body["invocations"]:
        listed.extend(invocation["memory_ids"])
    rendered = str(body)
    assert live["id"] in listed
    assert deleted["id"] not in listed
    assert other["id"] not in listed
    assert "PURPLE-MANGO-91" not in rendered
    assert "ORANGE-QUILL" not in rendered


def test_another_principal_cannot_read_the_goal(client: TestClient) -> None:
    body = _goal(client, PARALLEL)
    hidden = client.get(f"/v1/goals/{body['id']}", headers=auth("dev-restricted"))
    assert hidden.status_code == 404


def test_graph_validation_rejects_invalid_plans() -> None:
    agents = {
        "researcher": AgentRule("researcher", "researcher", "active", frozenset({"text.analyze"})),
        "verifier": AgentRule("verifier", "verifier", "active", frozenset()),
        "executor": AgentRule(
            "executor",
            "executor",
            "active",
            frozenset({"text.analyze", "records.export"}),
        ),
    }
    registry = build_default_registry()
    first = _node("a", "researcher", ("b",))
    second = _node("b", "researcher", ("a",))
    with pytest.raises(DomainError) as cycle:
        validate_graph([first, second], agents=agents, registry=registry, limit=8)
    assert cycle.value.code == "plan_invalid"
    with pytest.raises(DomainError) as self_dep:
        validate_graph(
            [_node("a", "researcher", ("a",))], agents=agents, registry=registry, limit=8
        )
    assert "itself" in self_dep.value.message
    with pytest.raises(DomainError):
        validate_graph(
            [_node("a", "researcher", ()), _node("a", "researcher", ())],
            agents=agents,
            registry=registry,
            limit=8,
        )
    with pytest.raises(DomainError):
        validate_graph(
            [_node("a", "researcher", ("missing",))], agents=agents, registry=registry, limit=8
        )
    with pytest.raises(DomainError):
        validate_graph(
            [_node(f"s{index}", "researcher", ()) for index in range(9)],
            agents=agents,
            registry=registry,
            limit=8,
        )
    unknown = GraphNode("a", "researcher", "x", "missing.tool", {"text": "one"}, (), {"kind": "x"})
    with pytest.raises(DomainError):
        validate_graph([unknown], agents=agents, registry=registry, limit=8)
    export = GraphNode(
        "a",
        "researcher",
        "x",
        "records.export",
        {"destination_label": "box", "content": {"note": "no"}},
        (),
        {"kind": "x"},
    )
    with pytest.raises(DomainError) as role:
        validate_graph([export], agents=agents, registry=registry, limit=8)
    assert role.value.code == "role_forbidden"
    disabled = dict(agents)
    disabled["researcher"] = AgentRule(
        "researcher", "researcher", "disabled", frozenset({"text.analyze"})
    )
    with pytest.raises(DomainError) as inactive:
        validate_graph([_node("a", "researcher", ())], agents=disabled, registry=registry, limit=8)
    assert inactive.value.code == "agent_unavailable"


def _goal(
    client: TestClient,
    objective: str,
    *,
    principal: str = "dev-user",
    project_key: str | None = None,
    max_waves: int | None = None,
) -> dict[str, object]:
    params = {} if max_waves is None else {"max_waves": max_waves}
    payload: dict[str, object] = {"objective": objective}
    if project_key is not None:
        payload["project_key"] = project_key
    response = client.post("/v1/goals", headers=auth(principal), json=payload, params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _memory(client: TestClient, project: str, content: str) -> dict[str, object]:
    response = client.post(
        "/v1/memory",
        headers=auth(),
        json={
            "scope_type": "project",
            "scope_id": project,
            "category": "fact",
            "content": content,
            "source_type": "user",
            "source_id": "note",
            "confirmed": True,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _node(key: str, agent: str, depends_on: tuple[str, ...]) -> GraphNode:
    return GraphNode(
        key=key,
        agent_key=agent,
        description="Count words.",
        capability_id="text.analyze",
        input_payload={"text": "one"},
        depends_on=depends_on,
        acceptance={"kind": "word_count"},
    )
