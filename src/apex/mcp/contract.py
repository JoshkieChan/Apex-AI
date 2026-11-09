"""Contracts for a future MCP client. This module does not speak MCP."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class McpToolDescriptor(BaseModel):
    """A tool a caller claims an MCP server exposed. It is not yet a capability."""

    model_config = ConfigDict(extra="forbid")

    server_name: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(min_length=1, max_length=128)
    description: str = Field(max_length=2000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None


class McpToolSource(Protocol):
    """A source of tool descriptors. Implementations must not execute tools themselves."""

    def list_tools(self) -> list[McpToolDescriptor]:
        """Return descriptors for review. Listing is not registration."""


class StaticToolSource:
    """Deterministic local source used to test the registration policy."""

    def __init__(self, tools: list[McpToolDescriptor]) -> None:
        self._tools = list(tools)

    def list_tools(self) -> list[McpToolDescriptor]:
        return list(self._tools)
