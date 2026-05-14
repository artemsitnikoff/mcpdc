"""MCP server implementation for Confluence."""

import logging

from mcp.server import Server

from .client import ConfluenceClient
from .tools import register_all_tools, ALL_TOOLS

logger = logging.getLogger(__name__)


def create_mcp_server(confluence_client: ConfluenceClient) -> Server:
    """Create and configure the MCP server."""
    server = Server("confluence-mcp")

    # Register all tools
    register_all_tools(server, confluence_client)

    # Add tool list to server for introspection
    @server.list_tools()
    async def list_tools():
        """List available tools."""
        return ALL_TOOLS

    logger.info(f"MCP server created with {len(ALL_TOOLS)} tools")
    return server