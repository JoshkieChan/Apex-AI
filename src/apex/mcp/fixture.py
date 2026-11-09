"""In-memory MCP fixture. It does not touch the host filesystem, shell, or network."""

from __future__ import annotations

from typing import Any

from mcp import Client
from mcp.server import MCPServer

from apex.errors import DomainError

LOOKUP_ID = "mcp.fixture.records_lookup"
UPDATE_ID = "mcp.fixture.records_update"
SERVER_KEY = "fixture"

_server = MCPServer("apex-fixture")
_records: dict[str, dict[str, str]] = {}
_applied: dict[str, dict[str, str]] = {}
_drop_after_write = False


def reset_fixture() -> None:
    """Restore the deterministic dataset and clear the fault hook."""
    global _drop_after_write
    _records.clear()
    _records.update(
        {
            "alpha": {"name": "alpha", "status": "open"},
            "beta": {"name": "beta", "status": "held"},
        }
    )
    _applied.clear()
    _drop_after_write = False


def set_drop_after_write(enabled: bool) -> None:
    """When enabled, an update is stored and then the call fails closed as uncertain."""
    global _drop_after_write
    _drop_after_write = enabled


def operation_landed(operation_id: str) -> dict[str, str] | None:
    """Return the update recorded for an operation id, without trusting a tool reply."""
    found = _applied.get(operation_id)
    if found is None:
        return None
    return dict(found)


def read_record(name: str) -> dict[str, str] | None:
    """Read fixture state without asking the tool whether it succeeded."""
    record = _records.get(name)
    if record is None:
        return None
    return dict(record)


def server() -> MCPServer:
    return _server


@_server.tool()
def records_lookup(name: str) -> dict[str, Any]:
    """Return one fixture record. The description is untrusted metadata."""
    record = _records.get(name)
    if record is None:
        return {"found": False, "name": name, "record": None}
    return {"found": True, "name": name, "record": dict(record)}


@_server.tool()
def records_update(name: str, status: str, operation_id: str) -> dict[str, Any]:
    """Update one fixture record. A repeated operation id returns the same result."""
    if name not in _records:
        raise ValueError("unknown record")
    prior = _applied.get(operation_id)
    if prior is not None:
        if prior["name"] != name or prior["status"] != status:
            raise ValueError("operation id was reused for a different update")
        return {"applied": True, "name": name, "status": status, "operation_id": operation_id}
    _records[name]["status"] = status
    _applied[operation_id] = {"name": name, "status": status}
    if _drop_after_write:
        raise RuntimeError("connection dropped after write")
    return {"applied": True, "name": name, "status": status, "operation_id": operation_id}


async def _list_tools() -> list[dict[str, Any]]:
    async with Client(server()) as client:
        listed = await client.list_tools()
    tools = getattr(listed, "tools", [])
    described: list[dict[str, Any]] = []
    for tool in tools:
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
        described.append(
            {
                "name": str(tool.name),
                "description": str(getattr(tool, "description", "") or ""),
                "input_schema": schema if isinstance(schema, dict) else {},
            }
        )
    return described


async def _call_tool(
    tool_name: str, arguments: dict[str, Any], timeout_seconds: float
) -> dict[str, Any]:
    async with Client(server()) as client:
        result = await client.call_tool(
            tool_name,
            arguments,
            read_timeout_seconds=timeout_seconds,
        )
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        raise DomainError("capability_failed", "The MCP tool reported an error.")
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if (
        isinstance(structured, dict)
        and set(structured) == {"result"}
        and isinstance(structured["result"], dict)
    ):
        return structured["result"]
    if isinstance(structured, dict):
        return structured
    raise DomainError("invalid_output", "The MCP tool did not return a structured object.")


def discover_fixture_tools() -> list[dict[str, Any]]:
    """List fixture tools through the MCP client. Listing does not register them."""
    import asyncio

    return asyncio.run(_list_tools())


def invoke_fixture_tool(
    tool_name: str,
    arguments: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Call one fixture tool through the MCP client."""
    import asyncio

    try:
        return asyncio.run(_call_tool(tool_name, arguments, timeout_seconds))
    except DomainError as exc:
        if _write_may_have_landed(tool_name, arguments):
            raise DomainError(
                "outcome_uncertain",
                "The update may have been applied before the connection failed.",
            ) from exc
        raise
    except Exception as exc:
        if _write_may_have_landed(tool_name, arguments):
            raise DomainError(
                "outcome_uncertain",
                "The update may have been applied before the connection failed.",
            ) from exc
        raise DomainError(
            "capability_unavailable", "The MCP server did not complete the call."
        ) from exc


def _write_may_have_landed(tool_name: str, arguments: dict[str, Any]) -> bool:
    operation_id = arguments.get("operation_id")
    return (
        tool_name == "records_update" and isinstance(operation_id, str) and operation_id in _applied
    )


reset_fixture()
