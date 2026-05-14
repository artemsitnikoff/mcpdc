"""Pytest configuration and fixtures."""

import pytest
import respx
from httpx import AsyncClient

from confluence_mcp.client import ConfluenceClient
from confluence_mcp.config import Settings


@pytest.fixture
def confluence_settings(tmp_path):
    """Create test settings.

    `confluence_download_dir` is pinned to a tmp directory so download-to-file
    tests don't pollute the repo `./downloads` sandbox.
    """
    return Settings(
        confluence_base_url="https://test.confluence.com",
        confluence_username="testuser",
        confluence_password="testpass",
        confluence_verify_ssl=False,
        host="localhost",
        port=8000,
        log_level="DEBUG",
        confluence_download_dir=str(tmp_path / "downloads"),
    )


@pytest.fixture
async def http_client():
    """Create async HTTP client."""
    async with AsyncClient() as client:
        yield client


@pytest.fixture
async def confluence_client(confluence_settings, http_client):
    """Create Confluence client."""
    client = ConfluenceClient(confluence_settings)
    client.set_http_client(http_client)
    return client


@pytest.fixture
def mock_confluence():
    """Create mock Confluence server with respx."""
    with respx.mock(base_url="https://test.confluence.com") as respx_mock:
        yield respx_mock


@pytest.fixture
def tool_handlers(confluence_client):
    """Collect MCP tool handlers by name, routed through the same code path
    that `register_all_tools` uses in production.

    The MCP SDK exposes a single global @server.call_tool() slot, so we can't
    register all 10 handlers on a real Server and call them by name. Instead,
    we reuse the `_HandlerCollector` proxy that production uses to gather
    handlers before installing the dispatcher.
    """
    from confluence_mcp.tools import _HandlerCollector
    from confluence_mcp.tools.attachments import register_attachment_tools
    from confluence_mcp.tools.comments import register_comment_tools
    from confluence_mcp.tools.pages import register_page_tools
    from confluence_mcp.tools.search import register_search_tools

    collector = _HandlerCollector()
    register_search_tools(collector, confluence_client)
    register_page_tools(collector, confluence_client)
    register_comment_tools(collector, confluence_client)
    register_attachment_tools(collector, confluence_client)
    return collector.handlers