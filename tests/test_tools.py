"""Tests for MCP tools."""

import pytest
import httpx

from confluence_mcp.tools import register_all_tools, ALL_TOOLS
from confluence_mcp.tools.search import register_search_tools
from confluence_mcp.tools.pages import register_page_tools
from confluence_mcp.tools.comments import register_comment_tools
from confluence_mcp.converters import storage_to_markdown


class TestToolFunctions:
    """Test individual tool functionality without MCP server."""

    async def test_search_tool_function(self, confluence_client, mock_confluence):
        """Test search functionality directly."""
        # Mock search response
        mock_confluence.get("/rest/api/content/search").mock(
            return_value=httpx.Response(200, json={
                "results": [
                    {
                        "id": "123456",
                        "title": "Test Page",
                        "type": "page",
                        "space": {"key": "TEST", "name": "Test Space"},
                        "_links": {"webui": "/spaces/TEST/pages/123456"},
                        "version": {"number": 1},
                        "excerpt": "This is a test page excerpt",
                    }
                ],
                "totalSize": 1,
            })
        )

        # Call search directly
        result = await confluence_client.search_content(
            "space = TEST",
            expand="space,version,excerpt"
        )

        assert result["totalSize"] == 1
        assert result["results"][0]["title"] == "Test Page"

    async def test_page_operations(self, confluence_client, mock_confluence):
        """Test page CRUD operations."""
        # Mock create
        mock_confluence.post("/rest/api/content").mock(
            return_value=httpx.Response(200, json={
                "id": "789012",
                "title": "New Test Page",
                "type": "page",
                "space": {"key": "TEST"},
                "version": {"number": 1, "when": "2024-01-01T00:00:00.000Z",
                         "by": {"displayName": "Test User"}},
                "_links": {"webui": "/spaces/TEST/pages/789012"},
            })
        )

        # Create page
        result = await confluence_client.create_content({
            "type": "page",
            "title": "New Test Page",
            "space": {"key": "TEST"},
            "body": {"storage": {"value": "<p>Content</p>", "representation": "storage"}},
        })

        assert result["title"] == "New Test Page"
        assert result["id"] == "789012"

    async def test_storage_to_markdown_conversion(self):
        """Test Storage Format to Markdown conversion."""
        storage_html = """
        <p>This is a <strong>bold</strong> paragraph.</p>
        <h1>Heading 1</h1>
        <ul>
            <li>Item 1</li>
            <li>Item 2</li>
        </ul>
        <ac:structured-macro ac:name="info">
            <ac:rich-text-body>
                <p>This is an info macro</p>
            </ac:rich-text-body>
        </ac:structured-macro>
        """

        markdown = storage_to_markdown(storage_html)

        assert "**bold**" in markdown
        assert "# Heading 1" in markdown
        assert "- Item 1" in markdown
        assert "- Item 2" in markdown
        assert "[INFO]" in markdown

    async def test_error_handling(self, confluence_client, mock_confluence):
        """Test error handling in client."""
        # Mock 404 error
        mock_confluence.get("/rest/api/content/nonexistent").mock(
            return_value=httpx.Response(404, json={"message": "Page not found"})
        )

        with pytest.raises(Exception) as exc_info:
            await confluence_client.get_content("nonexistent")

        assert "not found" in str(exc_info.value).lower()

    async def test_version_handling_in_update(self, confluence_client, mock_confluence):
        """Test automatic version handling in updates."""
        # Mock getting current version
        mock_confluence.get("/rest/api/content/123456").mock(
            return_value=httpx.Response(200, json={
                "id": "123456",
                "version": {"number": 5}
            })
        )

        # Mock successful update
        mock_confluence.put("/rest/api/content/123456").mock(
            return_value=httpx.Response(200, json={
                "id": "123456",
                "title": "Updated Page",
                "version": {"number": 6},
                "_links": {"webui": "/spaces/TEST/pages/123456"},
                "space": {"key": "TEST"},
            })
        )

        result = await confluence_client.update_content("123456", {"title": "Updated Page"})

        assert result["version"]["number"] == 6

    async def test_attachment_upload_headers(self, confluence_client, mock_confluence):
        """Test that file uploads include the required CSRF header."""
        mock_confluence.post("/rest/api/content/123456/child/attachment").mock(
            return_value=httpx.Response(200, json={
                "results": [
                    {
                        "id": "att123",
                        "title": "test.txt",
                        "version": {"number": 1, "when": "2024-01-01T00:00:00.000Z"},
                        "_links": {"download": "/download/attachments/123456/test.txt"},
                    }
                ]
            })
        )

        result = await confluence_client.upload_attachment(
            "123456", "test.txt", b"test content"
        )

        # Verify the request was made with the right header
        post_calls = [call for call in mock_confluence.calls if call.request.method == "POST"]
        assert len(post_calls) == 1
        assert post_calls[0].request.headers["X-Atlassian-Token"] == "no-check"

        assert "results" in result


class TestDispatcher:
    """Regression tests for the call_tool dispatcher.

    The MCP SDK exposes a single global @server.call_tool() slot; without a
    routing dispatcher, registering 10 handlers leaves only the last one alive.
    """

    def test_all_tools_have_handlers(self, confluence_client):
        """Every tool advertised in ALL_TOOLS must be addressable through dispatch."""
        captured_dispatch = {}

        class FakeServer:
            def __init__(self):
                self.dispatch_handler = None

            def call_tool(self):
                def decorator(func):
                    captured_dispatch["fn"] = func
                    return func
                return decorator

        fake = FakeServer()
        register_all_tools(fake, confluence_client)

        # The single dispatcher was installed
        assert "fn" in captured_dispatch, "register_all_tools did not register a dispatcher"

        # Every advertised tool must be routable. We inspect the closure to find
        # the handlers map the dispatcher closes over.
        fn = captured_dispatch["fn"]
        closure_cells = list(fn.__closure__ or ())
        handler_maps = [c.cell_contents for c in closure_cells if isinstance(c.cell_contents, dict) and c.cell_contents]
        assert handler_maps, "Dispatcher does not close over a handlers dict"
        handlers = handler_maps[0]

        advertised = {t.name for t in ALL_TOOLS}
        assert advertised <= set(handlers.keys()), (
            f"Tools advertised but not dispatchable: {advertised - set(handlers.keys())}"
        )

    async def test_dispatcher_routes_unknown_tool(self, confluence_client):
        captured = {}

        class FakeServer:
            def call_tool(self):
                def decorator(func):
                    captured["fn"] = func
                    return func
                return decorator

        register_all_tools(FakeServer(), confluence_client)
        result = await captured["fn"]("does_not_exist", {})
        # Should return a graceful error, not raise
        assert result and "Unknown tool" in result[0].text


class TestDownloadSandbox:
    """The download-to-file tool must keep writes inside the configured sandbox."""

    def test_relative_path_lands_inside_sandbox(self, tmp_path):
        from confluence_mcp.tools.attachments import _resolve_download_path
        target = _resolve_download_path("sub/dir/file.txt", str(tmp_path))
        assert str(target).startswith(str(tmp_path.resolve()))
        assert target.name == "file.txt"

    def test_dotdot_traversal_is_rejected(self, tmp_path):
        from confluence_mcp.tools.attachments import _resolve_download_path
        from confluence_mcp.errors import ConfluencePathError
        with pytest.raises(ConfluencePathError):
            _resolve_download_path("../../../etc/passwd_hijack", str(tmp_path))

    def test_absolute_path_is_reinterpreted_as_relative(self, tmp_path):
        from confluence_mcp.tools.attachments import _resolve_download_path
        # An absolute /etc/passwd from a caller MUST NOT escape the sandbox.
        target = _resolve_download_path("/etc/passwd", str(tmp_path))
        assert str(target).startswith(str(tmp_path.resolve()))
        assert "etc" in target.parts or target.name == "passwd"


class TestToolSchemas:
    """Test tool schema definitions."""

    def test_search_tool_schema(self):
        """Test search tool schema is valid."""
        from confluence_mcp.tools.search import SEARCH_TOOLS

        search_tool = SEARCH_TOOLS[0]
        assert search_tool.name == "confluence_search"
        assert "cql" in search_tool.inputSchema["required"]

    def test_page_tool_schemas(self):
        """Test page tool schemas are valid."""
        from confluence_mcp.tools.pages import PAGE_TOOLS

        tool_names = [tool.name for tool in PAGE_TOOLS]
        expected_tools = [
            "confluence_get_page",
            "confluence_create_page",
            "confluence_update_page",
            "confluence_delete_page",
        ]

        for expected in expected_tools:
            assert expected in tool_names

    def test_comment_tool_schemas(self):
        """Test comment tool schemas are valid."""
        from confluence_mcp.tools.comments import COMMENT_TOOLS

        tool_names = [tool.name for tool in COMMENT_TOOLS]
        expected_tools = [
            "confluence_list_comments",
            "confluence_add_comment",
        ]

        for expected in expected_tools:
            assert expected in tool_names

    def test_attachment_tool_schemas(self):
        """Test attachment tool schemas are valid."""
        from confluence_mcp.tools.attachments import ATTACHMENT_TOOLS

        tool_names = [tool.name for tool in ATTACHMENT_TOOLS]
        expected_tools = [
            "confluence_list_attachments",
            "confluence_download_attachment",
            "confluence_upload_attachment",
        ]

        for expected in expected_tools:
            assert expected in tool_names