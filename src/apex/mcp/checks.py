"""Deterministic checks that read fixture state instead of trusting the tool response."""

from __future__ import annotations

from typing import Any

from apex.contracts.schemas import CheckResult
from apex.mcp.fixture import read_record
from apex.verification.engine import register_checker


def install_mcp_checks() -> None:
    """Register checkers for the local fixture capabilities."""
    register_checker("records.lookup", _lookup)
    register_checker("records.update", _update)


def _lookup(payload: dict[str, Any], output: dict[str, Any]) -> list[CheckResult]:
    name = payload.get("name")
    if not isinstance(name, str):
        return [
            CheckResult(name="name_present", passed=False, detail="The record name is missing.")
        ]
    actual = read_record(name)
    expected_found = actual is not None
    return [
        CheckResult(
            name="found",
            passed=output.get("found") is expected_found,
            detail="The lookup result must match the fixture store.",
        ),
        CheckResult(
            name="record",
            passed=output.get("record") == actual,
            detail="The returned record must match the fixture store.",
        ),
    ]


def _update(payload: dict[str, Any], output: dict[str, Any]) -> list[CheckResult]:
    name = payload.get("name")
    status = payload.get("status")
    if not isinstance(name, str) or not isinstance(status, str):
        return [
            CheckResult(
                name="input_present", passed=False, detail="The update input is incomplete."
            )
        ]
    actual = read_record(name)
    stored = actual.get("status") if actual else None
    return [
        CheckResult(
            name="tool_claim",
            passed=output.get("applied") is True and output.get("status") == status,
            detail="The tool claim must describe the requested status.",
        ),
        CheckResult(
            name="stored_status",
            passed=stored == status,
            detail="The fixture store must contain the requested status.",
        ),
    ]
