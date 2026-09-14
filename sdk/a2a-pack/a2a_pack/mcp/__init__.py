"""Model Context Protocol (MCP) adapter.

Exposes any :class:`A2AAgent` as an MCP server so its skills are usable
from Claude Code, Cursor, and any other MCP client.

Agents are deployed to the cloud; clients connect to ``POST /mcp`` on
the agent's public URL (MCP "Streamable HTTP" transport).
:func:`a2a_pack.serve.build_app` auto-mounts this endpoint, so every
shipped agent is an MCP server with no extra wiring.
"""
from .server import (
    MCP_PROTOCOL_VERSION,
    MCPServer,
    skills_to_tools,
    tool_call_result,
    tool_call_error,
)
from .http import build_http_app, mount_connector_http, mount_http

__all__ = [
    "MCP_PROTOCOL_VERSION",
    "MCPServer",
    "build_http_app",
    "mount_connector_http",
    "mount_http",
    "skills_to_tools",
    "tool_call_error",
    "tool_call_result",
]
