"""Focused unit tests for tool-package internals.

What used to live here:
- Client-level CRUD/error scenarios (search, page ops, version handling,
  upload header, 404 mapping). Those duplicated `tests/test_client.py` at
  the same abstraction.
- A dispatcher-internals test that introspected `_dispatch.__closure__` —
  fragile to any local-variable reordering, and covered by the public
  `tool_handlers` fixture path in `test_handlers.py`.

What stays:
- `test_storage_to_markdown_conversion` — the only unit test of
  `converters.storage_to_markdown`.
- `TestDownloadSandbox` — exercises `_resolve_download_path` (traversal,
  absolute-path reinterpretation) without going through the full handler.
- `TestToolSchemas` — schema-shape regressions (presence of every tool
  name and required fields). Cheap structural cover.

End-to-end behaviour through the MCP dispatcher lives in
`tests/test_handlers.py`; HTTP-client behaviour in `tests/test_client.py`.
"""

import pytest

from confluence_mcp.converters import storage_to_markdown


async def test_storage_to_markdown_conversion():
    """Storage Format → Markdown smoke test, including a Confluence macro.

    Macros are converted with custom logic in `_convert_confluence_macros`,
    so we want one path through that branch covered alongside the plain
    HTML mapping handled by `markdownify`.
    """
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
    """Test tool schema definitions — schema-shape regressions only.

    Behavioural coverage of the tools themselves lives in `test_handlers.py`.
    """

    def test_search_tool_schema(self):
        from confluence_mcp.tools.search import SEARCH_TOOLS

        search_tool = SEARCH_TOOLS[0]
        assert search_tool.name == "confluence_search"
        assert "cql" in search_tool.inputSchema["required"]

    def test_page_tool_schemas(self):
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
        from confluence_mcp.tools.comments import COMMENT_TOOLS

        tool_names = [tool.name for tool in COMMENT_TOOLS]
        expected_tools = [
            "confluence_list_comments",
            "confluence_add_comment",
        ]

        for expected in expected_tools:
            assert expected in tool_names

    def test_attachment_tool_schemas(self):
        from confluence_mcp.tools.attachments import ATTACHMENT_TOOLS

        tool_names = [tool.name for tool in ATTACHMENT_TOOLS]
        expected_tools = [
            "confluence_list_attachments",
            "confluence_download_attachment",
            "confluence_upload_attachment",
        ]

        for expected in expected_tools:
            assert expected in tool_names

    def test_no_top_level_combinators_in_any_input_schema(self):
        """Anthropic API rejects oneOf/allOf/anyOf at the root of input_schema.

        Regression 2026-05-14: top-level `anyOf` in `confluence_get_page` made
        every tools/* call fail with `400 tools.N.custom.input_schema`.
        """
        from confluence_mcp.tools import ALL_TOOLS

        forbidden = {"oneOf", "allOf", "anyOf"}
        offenders = [
            (t.name, k) for t in ALL_TOOLS
            for k in forbidden
            if k in (t.inputSchema or {})
        ]
        assert not offenders, (
            f"Top-level combinators forbidden by Anthropic API: {offenders}"
        )
