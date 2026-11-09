"""Turn a discovered MCP descriptor into a non-executable proposal."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from apex.mcp.contract import McpToolDescriptor


class CapabilityProposal(BaseModel):
    """A reviewed descriptor that still requires explicit registration before use."""

    model_config = ConfigDict(extra="forbid")

    capability_id: str
    server_name: str
    tool_name: str
    auto_register: bool
    available: bool
    reason: str


def review_discovered_tool(tool: McpToolDescriptor) -> CapabilityProposal:
    """Refuse automatic registration. An operator must register an approved adapter later."""
    return CapabilityProposal(
        capability_id=f"mcp.{tool.server_name}.{tool.tool_name}",
        server_name=tool.server_name,
        tool_name=tool.tool_name,
        auto_register=False,
        available=False,
        reason="Discovered MCP tools are not executable until they are explicitly registered.",
    )
