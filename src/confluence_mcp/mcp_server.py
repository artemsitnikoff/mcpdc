"""MCP server implementation for Confluence."""

import logging

from mcp.server import Server

from .tools import register_all_tools, ALL_TOOLS

logger = logging.getLogger(__name__)


def create_mcp_server() -> Server:
    """Create and configure the MCP server.

    The server is shared across all SSE sessions; per-session Confluence
    clients are looked up via contextvar inside each tool dispatch, so the
    server itself is credential-agnostic.
    """
    server = Server("confluence-mcp")

    register_all_tools(server)

    @server.list_tools()
    async def list_tools():
        """List available tools."""
        return ALL_TOOLS

    logger.info(f"MCP server created with {len(ALL_TOOLS)} tools")
    return server