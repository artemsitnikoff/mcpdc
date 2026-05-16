"""Pytest configuration and fixtures."""

import pytest
import respx
from httpx import AsyncClient

from confluence_mcp.client import ConfluenceClient
from confluence_mcp.config import Settings
from confluence_mcp.session import current_confluence_client


@pytest.fixture
def confluence_settings(tmp_path):
    """Server-level Settings (no credentials — those are per-session).

    `confluence_download_dir` is pinned to a tmp directory so download-to-file
    tests don't pollute the repo `./downloads` sandbox.
    """
    return Settings(
        confluence_base_url="https://test.confluence.com",
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
    """Build a ConfluenceClient with stub per-session credentials.

    The credentials are placeholders — respx-mocked Confluence never actually
    checks the Authorization header. Tests that need to assert on the
    *outgoing* Basic-Auth value should make their own client.
    """
    client = ConfluenceClient(confluence_settings, "testuser", "testpass")
    client.set_http_client(http_client)
    return client


@pytest.fixture
def mock_confluence():
    """Create mock Confluence server with respx."""
    with respx.mock(base_url="https://test.confluence.com") as respx_mock:
        yield respx_mock


@pytest.fixture
def tool_handlers(confluence_client):
    """Collect MCP tool handlers by name, with the contextvar pre-set.

    The MCP SDK exposes a single global @server.call_tool() slot, so we can't
    register all 10 handlers on a real Server and call them by name. Instead,
    we reuse the `_HandlerCollector` proxy that production uses to gather
    handlers before installing the dispatcher.

    Production tools resolve their Confluence client through
    `LazyConfluenceClient`, which reads `current_confluence_client`. The
    fixture sets that contextvar to the test client for the test's lifetime,
    so handlers find a real client when they call methods on the proxy.
    """
    from confluence_mcp.session import LazyConfluenceClient
    from confluence_mcp.tools import _HandlerCollector
    from confluence_mcp.tools.attachments import register_attachment_tools
    from confluence_mcp.tools.comments import register_comment_tools
    from confluence_mcp.tools.pages import register_page_tools
    from confluence_mcp.tools.search import register_search_tools

    token = current_confluence_client.set(confluence_client)
    try:
        collector = _HandlerCollector()
        proxy = LazyConfluenceClient()
        register_search_tools(collector, proxy)
        register_page_tools(collector, proxy)
        register_comment_tools(collector, proxy)
        register_attachment_tools(collector, proxy)
        yield collector.handlers
    finally:
        current_confluence_client.reset(token)
