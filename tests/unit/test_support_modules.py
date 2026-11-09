"""Redaction, lifecycle rules, MCP policy, and import boundaries."""

from __future__ import annotations

import ast
import logging
from datetime import UTC
from pathlib import Path

import pytest

from apex.contracts.schemas import LEGAL_TRANSITIONS, TERMINAL_TASK_STATUSES, TaskStatus
from apex.errors import DomainError
from apex.logging_config import JsonFormatter
from apex.mcp.contract import McpToolDescriptor, StaticToolSource
from apex.mcp.policy import review_discovered_tool
from apex.redaction import redact
from apex.runtime.transitions import transition

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "apex"

FORBIDDEN_IMPORTS = {
    "apex.api": ("apex.execution", "apex.verification"),
    "apex.intent": ("apex.execution", "apex.api", "apex.runtime"),
    "apex.planning": ("apex.execution", "apex.authority", "apex.api", "apex.runtime"),
    "apex.verification": ("apex.execution", "apex.api", "apex.runtime"),
    "apex.execution": ("apex.api", "apex.runtime", "apex.intent", "apex.planning"),
    "apex.providers": ("apex.execution", "apex.api", "apex.runtime", "apex.authority"),
    "apex.authority": ("apex.execution", "apex.runtime", "apex.api"),
}

HOST_EXECUTION = {"subprocess", "socket"}


def test_redaction_removes_credentials() -> None:
    raw = (
        "openai sk-abcdefghijklmnopqrstuvwxyz Bearer super-secret-token "
        "api_key=abcdefg postgresql://apex:apex@localhost/apex"
    )
    cleaned = redact(raw)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in cleaned
    assert "super-secret-token" not in cleaned
    assert "abcdefg" not in cleaned
    assert "apex:apex@" not in cleaned
    assert "[REDACTED]" in cleaned


def test_json_formatter_redacts_records() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="apex.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="leaked sk-abcdefghijklmnopqrstuvwxyz",
        args=(),
        exc_info=None,
    )
    rendered = formatter.format(record)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in rendered
    assert "REDACTED" in rendered


def test_terminal_states_have_no_outbound_transitions() -> None:
    for status in TERMINAL_TASK_STATUSES:
        assert LEGAL_TRANSITIONS[status] == frozenset()
    assert TaskStatus.uncertain not in TERMINAL_TASK_STATUSES
    assert LEGAL_TRANSITIONS[TaskStatus.uncertain] == frozenset(
        {TaskStatus.completed, TaskStatus.failed}
    )
    assert TaskStatus.executing not in LEGAL_TRANSITIONS[TaskStatus.uncertain]
    assert TaskStatus.executing in LEGAL_TRANSITIONS[TaskStatus.planned]
    with pytest.raises(DomainError):
        transition(
            None,
            _FakeTask(TaskStatus.completed.value),
            TaskStatus.executing,
            event_type="x",
            actor_id=None,
            payload={},
            clock=_Clock(),
        )  # type: ignore[arg-type]


def test_discovered_mcp_tool_is_not_executable() -> None:
    source = StaticToolSource(
        [
            McpToolDescriptor(
                server_name="local",
                tool_name="files",
                description="A discovered tool.",
                input_schema={"type": "object"},
            )
        ]
    )
    proposal = review_discovered_tool(source.list_tools()[0])
    assert proposal.auto_register is False
    assert proposal.available is False
    registry_ids = {
        spec.id
        for spec in __import__("apex.capabilities.builtins", fromlist=["build_default_registry"])
        .build_default_registry()
        .list()
    }
    assert proposal.capability_id not in registry_ids


def test_import_boundaries_and_no_host_execution() -> None:
    for path in SRC.rglob("*.py"):
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                for package, banned in FORBIDDEN_IMPORTS.items():
                    if module == package or module.startswith(package + "."):
                        assert not any(
                            name == item or name.startswith(item + ".") for item in banned
                        ), f"{module} imports {name}"
                if _in_restricted_runtime(module):
                    assert name not in HOST_EXECUTION
            if _in_restricted_runtime(module) and isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in {"system", "popen"}:
                    raise AssertionError(f"{module} calls {func.attr}")


def _module_name(path: Path) -> str:
    relative = path.relative_to(SRC.parent).with_suffix("")
    return ".".join(relative.parts)


def _in_restricted_runtime(module: str) -> bool:
    prefixes = (
        "apex.capabilities",
        "apex.execution",
        "apex.authority",
        "apex.verification",
        "apex.runtime",
        "apex.planning",
        "apex.intent",
        "apex.memory",
        "apex.agents",
        "apex.mcp",
    )
    return module == "apex" or any(
        module == prefix or module.startswith(prefix + ".") for prefix in prefixes
    )


class _FakeTask:
    def __init__(self, status: str) -> None:
        self.status = status
        self.id = "task"


class _Clock:
    def now(self) -> object:
        from datetime import datetime

        return datetime.now(UTC)
