"""Tools package for Confluence MCP server.

The MCP Python SDK exposes a single `@server.call_tool()` slot — registering
multiple handlers overwrites it. Each tool module here was written to register
its own handler, so we collect them via a Server-shaped proxy and install one
global dispatcher that routes by tool name.
"""

import logging
from typing import Any, Callable, Dict, List

from mcp.server import Server
from mcp.types import TextContent

from ..client import ConfluenceClient
from .search import register_search_tools, SEARCH_TOOLS
from .pages import register_page_tools, PAGE_TOOLS
from .comments import register_comment_tools, COMMENT_TOOLS
from .attachments import register_attachment_tools, ATTACHMENT_TOOLS

logger = logging.getLogger(__name__)

ALL_TOOLS = SEARCH_TOOLS + PAGE_TOOLS + COMMENT_TOOLS + ATTACHMENT_TOOLS


class _HandlerCollector:
    """Quacks like `mcp.server.Server` for the `call_tool` decorator only."""

    def __init__(self) -> None:
        self.handlers: Dict[str, Callable] = {}

    def call_tool(self):
        def decorator(func):
            self.handlers[func.__name__] = func
            return func
        return decorator


def register_all_tools(server: Server, confluence: ConfluenceClient) -> None:
    collector = _HandlerCollector()
    register_search_tools(collector, confluence)
    register_page_tools(collector, confluence)
    register_comment_tools(collector, confluence)
    register_attachment_tools(collector, confluence)

    handlers = collector.handlers
    logger.info(f"Collected {len(handlers)} tool handlers: {sorted(handlers)}")

    @server.call_tool()
    async def _dispatch(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
        handler = handlers.get(name)
        if handler is None:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
        return await handler(arguments)
