"""End-to-end tests for MCP tool handlers (search, pages, comments, attachments).

These tests drive each handler through the same `_HandlerCollector` path that
production uses, asserting on the *text* returned in `TextContent`. The intent
is to catch a class of regressions that the existing client-level tests miss:

- handlers that pass type checks but stub out the user-facing response
- error branches that never get exercised (404, validation, missing args)
- format-rendering bugs (Storage vs Markdown, indented comment bodies)
- the silent-no-op around `update_page(content="")`

If a future change to a tool breaks the public contract, these tests should
turn red even when `tests/test_client.py` stays green.
"""

import base64

import httpx
import pytest

from confluence_mcp.tools import ALL_TOOLS


# ---------------------------------------------------------------------------
# Fixtures helpers
# ---------------------------------------------------------------------------

_PAGE_JSON = {
    "id": "123456",
    "title": "Test Page",
    "type": "page",
    "space": {"key": "PBZ", "name": "Knowledge Base"},
    "body": {"storage": {"value": "<p>hello</p>", "representation": "storage"}},
    "version": {
        "number": 7,
        "when": "2026-05-12T00:00:00.000Z",
        "by": {"displayName": "Integration User"},
    },
    "_links": {"webui": "/spaces/PBZ/pages/123456"},
}


def _page(overrides=None):
    out = {**_PAGE_JSON}
    if overrides:
        out.update(overrides)
    return out


# ---------------------------------------------------------------------------
# Dispatcher coverage
# ---------------------------------------------------------------------------

class TestDispatcherCoverage:
    """Every advertised tool must have a callable handler reachable by name."""

    def test_collector_yields_handler_per_advertised_tool(self, tool_handlers):
        advertised = {t.name for t in ALL_TOOLS}
        assert advertised == set(tool_handlers.keys()), (
            f"Mismatch: advertised={advertised} collected={set(tool_handlers)}"
        )

    async def test_dispatcher_returns_graceful_error_for_unknown_tool(
        self, confluence_client
    ):
        """The real `_dispatch` registered by `register_all_tools` must answer
        an unknown name with a TextContent payload, not raise.

        Regression guard: if someone replaces `handlers.get(name)` with
        `handlers[name]`, MCP clients would see the session crash instead of
        a friendly "Unknown tool" message. Captures the *actual* dispatcher
        via a Server-shaped proxy (the same trick the `tool_handlers` fixture
        uses, just at the outer layer).
        """
        from confluence_mcp.tools import register_all_tools

        captured = {}

        class FakeServer:
            def call_tool(self):
                def decorator(func):
                    captured["fn"] = func
                    return func
                return decorator

        register_all_tools(FakeServer(), confluence_client)
        assert "fn" in captured, "register_all_tools did not install a dispatcher"

        result = await captured["fn"]("does_not_exist", {})
        assert result, "dispatcher must return at least one TextContent"
        assert result[0].type == "text"
        assert "Unknown tool" in result[0].text
        assert "does_not_exist" in result[0].text


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class TestSearchHandler:
    async def test_returns_text_content_with_results(self, tool_handlers, mock_confluence):
        mock_confluence.get("/rest/api/content/search").mock(
            return_value=httpx.Response(200, json={
                "results": [{
                    "id": "1",
                    "title": "Foo",
                    "type": "page",
                    "space": {"key": "PBZ", "name": "KB"},
                    "_links": {"webui": "/x"},
                    "version": {"number": 1},
                    "excerpt": "blurb",
                }],
                "totalSize": 1,
            })
        )

        result = await tool_handlers["confluence_search"]({"cql": "type=page", "limit": 5})

        assert result[0].type == "text"
        text = result[0].text
        assert "Search Results" in text
        assert "Foo" in text
        assert "PBZ" in text

    async def test_missing_cql_returns_error_text(self, tool_handlers):
        result = await tool_handlers["confluence_search"]({})
        assert result[0].type == "text"
        assert "Error" in result[0].text
        assert "cql" in result[0].text


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

class TestGetPageHandler:
    async def test_get_by_id_storage_format(self, tool_handlers, mock_confluence):
        mock_confluence.get("/rest/api/content/123456").mock(
            return_value=httpx.Response(200, json=_page())
        )

        result = await tool_handlers["confluence_get_page"]({"id": "123456"})

        assert result[0].type == "text"
        text = result[0].text
        assert "Page Retrieved" in text
        assert "Test Page" in text
        assert "<p>hello</p>" in text  # storage format passes through
        assert "Version: 7" in text

    async def test_get_by_id_markdown_format(self, tool_handlers, mock_confluence):
        mock_confluence.get("/rest/api/content/123456").mock(
            return_value=httpx.Response(200, json=_page())
        )

        result = await tool_handlers["confluence_get_page"](
            {"id": "123456", "format": "markdown"}
        )

        text = result[0].text
        assert "hello" in text  # markdown rendering
        # `Content Format: markdown` precedes the body separator `Content:` —
        # take everything AFTER the last `Content:` so the assert reflects the
        # rendered body, not the metadata line above it.
        body = text.rsplit("Content:", 1)[-1]
        assert "<p>" not in body, (
            f"Storage markup must be stripped in markdown mode; body was:\n{body}"
        )

    async def test_validation_no_id_no_title_returns_error(self, tool_handlers):
        result = await tool_handlers["confluence_get_page"]({})
        assert "Error" in result[0].text
        assert "id" in result[0].text or "space_key" in result[0].text

    async def test_invalid_format_returns_error(self, tool_handlers):
        result = await tool_handlers["confluence_get_page"](
            {"id": "1", "format": "yaml"}
        )
        assert "Error" in result[0].text
        assert "format" in result[0].text

    async def test_not_found_returns_friendly_text(self, tool_handlers, mock_confluence):
        mock_confluence.get("/rest/api/content/999").mock(
            return_value=httpx.Response(404, json={"message": "no such page"})
        )
        result = await tool_handlers["confluence_get_page"]({"id": "999"})
        assert result[0].type == "text"
        assert "Page not found" in result[0].text

    async def test_get_by_space_and_title(self, tool_handlers, mock_confluence):
        # Match on `params=` so a future typo (e.g. `space=` instead of
        # `spaceKey=`) breaks the test — without this matcher respx would
        # accept any querystring and silently mask the regression.
        mock_confluence.get(
            "/rest/api/content",
            params={"spaceKey": "PBZ", "title": "Test Page", "expand": "body.storage,space,version"},
        ).mock(return_value=httpx.Response(200, json={"results": [_page()]}))

        result = await tool_handlers["confluence_get_page"](
            {"space_key": "PBZ", "title": "Test Page"}
        )
        assert "Page Retrieved" in result[0].text


class TestCreatePageHandler:
    async def test_create_returns_page_info(self, tool_handlers, mock_confluence):
        mock_confluence.post("/rest/api/content").mock(
            return_value=httpx.Response(200, json=_page({"id": "new-1"}))
        )
        result = await tool_handlers["confluence_create_page"](
            {"space_key": "PBZ", "title": "New", "content": "<p>x</p>"}
        )
        text = result[0].text
        assert "Page Created Successfully" in text
        assert "new-1" in text

    async def test_create_missing_required_returns_error(self, tool_handlers):
        result = await tool_handlers["confluence_create_page"]({"title": "x"})
        assert "Error" in result[0].text
        # Make sure the user is told *what* is missing, not just that something
        # went wrong — guards against the message drifting to a generic blob.
        assert "space_key" in result[0].text

    async def test_create_403_surfaces_permission_error(self, tool_handlers, mock_confluence):
        # The PM space is read-only for the dclouds service account; create → 403.
        mock_confluence.post("/rest/api/content").mock(
            return_value=httpx.Response(403, json={"message": "PM space is read-only"})
        )
        result = await tool_handlers["confluence_create_page"](
            {"space_key": "PM", "title": "X", "content": "<p>x</p>"}
        )
        assert "Failed to create" in result[0].text
        assert "Permission denied" in result[0].text


class TestUpdatePageHandler:
    async def test_explicit_empty_content_is_honored_not_silently_kept(
        self, tool_handlers, mock_confluence
    ):
        """Regression: previously `new_content or current_value` silently swapped
        an explicit "" back to the existing content, so update succeeded without
        actually changing anything.
        """
        captured = {}

        mock_confluence.get("/rest/api/content/123456").mock(
            return_value=httpx.Response(200, json=_page())
        )

        def _capture(request):
            import json
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json=_page({"version": {
                "number": 8,
                "when": "2026-05-14T00:00:00.000Z",
                "by": {"displayName": "Integration User"},
            }}))

        mock_confluence.put("/rest/api/content/123456").mock(side_effect=_capture)

        await tool_handlers["confluence_update_page"]({"id": "123456", "content": ""})

        assert captured["body"]["body"]["storage"]["value"] == "", (
            "Empty content must reach Confluence verbatim, not be replaced by "
            "the existing value"
        )

    async def test_update_without_id_returns_error(self, tool_handlers):
        result = await tool_handlers["confluence_update_page"]({"title": "x"})
        assert "Error" in result[0].text
        assert "id" in result[0].text

    async def test_update_without_any_field_returns_error(self, tool_handlers):
        result = await tool_handlers["confluence_update_page"]({"id": "1"})
        assert "Error" in result[0].text


class TestDeletePageHandler:
    async def test_delete_returns_confirmation(self, tool_handlers, mock_confluence):
        mock_confluence.get("/rest/api/content/123456").mock(
            return_value=httpx.Response(200, json=_page())
        )
        mock_confluence.delete("/rest/api/content/123456").mock(
            return_value=httpx.Response(204)
        )
        result = await tool_handlers["confluence_delete_page"]({"id": "123456"})
        assert "Page Deleted Successfully" in result[0].text


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

class TestCommentsHandlers:
    async def test_list_comments_returns_text(self, tool_handlers, mock_confluence):
        mock_confluence.get("/rest/api/content/123456/child/comment").mock(
            return_value=httpx.Response(200, json={
                "results": [{
                    "id": "c-1",
                    "title": "Re: page",
                    "version": {
                        "number": 1,
                        "when": "2026-05-14T00:00:00.000Z",
                        "by": {"displayName": "u"},
                    },
                    "body": {"storage": {"value": "<p>nice</p>"}},
                    "_links": {"webui": "/c-1"},
                }],
                "totalSize": 1,
            })
        )
        result = await tool_handlers["confluence_list_comments"]({"page_id": "123456"})
        text = result[0].text
        assert "Comments" in text
        assert "c-1" in text

    async def test_add_comment_sends_container_type_page(
        self, tool_handlers, mock_confluence
    ):
        """Regression: Confluence 7.4.6 requires `container.type=page` on POST,
        otherwise it returns 500. CLAUDE.md, gotcha #1.
        """
        captured = {}

        def _capture(request):
            import json
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={
                "id": "c-new",
                "version": {
                    "number": 1,
                    "when": "2026-05-14T00:00:00.000Z",
                    "by": {"displayName": "u"},
                },
                "_links": {"webui": "/c-new"},
            })

        mock_confluence.post("/rest/api/content").mock(side_effect=_capture)

        await tool_handlers["confluence_add_comment"](
            {"page_id": "123456", "content": "<p>hi</p>"}
        )

        assert captured["body"]["container"] == {"id": "123456", "type": "page"}
        assert captured["body"]["type"] == "comment"


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

class TestAttachmentsHandlers:
    async def test_list_attachments(self, tool_handlers, mock_confluence):
        mock_confluence.get("/rest/api/content/123456/child/attachment").mock(
            return_value=httpx.Response(200, json={
                "results": [{
                    "id": "att-1",
                    "title": "doc.pdf",
                    "metadata": {"mediaType": "application/pdf", "fileSize": 1234},
                    "version": {
                        "number": 1,
                        "when": "2026-05-14T00:00:00.000Z",
                        "by": {"displayName": "u"},
                    },
                    "_links": {"download": "/download/attachments/123456/doc.pdf"},
                }],
                "totalSize": 1,
            })
        )
        result = await tool_handlers["confluence_list_attachments"]({"page_id": "123456"})
        assert "Attachments" in result[0].text
        assert "doc.pdf" in result[0].text

    async def test_download_attachment_base64(self, tool_handlers, mock_confluence):
        mock_confluence.get(
            "/download/attachments/123456/doc.pdf"
        ).mock(return_value=httpx.Response(200, content=b"PDFDATA"))

        result = await tool_handlers["confluence_download_attachment"](
            {"page_id": "123456", "filename": "doc.pdf"}
        )
        text = result[0].text
        assert "Attachment Downloaded" in text
        # Positive invariants:
        #   - the formatter labelled the payload as base64 (the contract)
        #   - did NOT take the file-output branch
        #   - the preview is the actual base64 of our mock content
        assert "Base64 Content" in text
        assert "Saved to:" not in text
        assert "Output: base64" in text
        assert base64.b64encode(b"PDFDATA").decode() in text

    async def test_download_attachment_to_file_uses_sandbox(
        self, tool_handlers, mock_confluence, confluence_settings
    ):
        mock_confluence.get(
            "/download/attachments/123456/doc.pdf"
        ).mock(return_value=httpx.Response(200, content=b"PDFDATA"))

        result = await tool_handlers["confluence_download_attachment"]({
            "page_id": "123456",
            "filename": "doc.pdf",
            "output": "file",
            "file_path": "out/here.pdf",
        })
        text = result[0].text
        assert "Saved to:" in text
        # The resolved path must be under the configured sandbox.
        from pathlib import Path
        expected_root = Path(confluence_settings.confluence_download_dir).resolve()
        assert str(expected_root) in text

    async def test_download_traversal_path_is_refused(
        self, tool_handlers, mock_confluence
    ):
        mock_confluence.get(
            "/download/attachments/123456/doc.pdf"
        ).mock(return_value=httpx.Response(200, content=b"PDFDATA"))

        result = await tool_handlers["confluence_download_attachment"]({
            "page_id": "123456",
            "filename": "doc.pdf",
            "output": "file",
            "file_path": "../../escape.pdf",
        })
        assert "Refused" in result[0].text

    async def test_upload_size_limit_refused_before_network(
        self, tool_handlers, confluence_settings, tmp_path, mock_confluence
    ):
        # Create a file slightly above the configured upload limit.
        limit = confluence_settings.confluence_max_upload_bytes
        too_big = tmp_path / "big.bin"
        too_big.write_bytes(b"x" * (limit + 1))

        result = await tool_handlers["confluence_upload_attachment"](
            {"page_id": "123456", "file_path": str(too_big)}
        )

        assert "Refused" in result[0].text
        # And no HTTP call should have left the box.
        assert not mock_confluence.calls, "Upload must short-circuit before any HTTP request"

    async def test_upload_sends_csrf_header(
        self, tool_handlers, mock_confluence, tmp_path
    ):
        small = tmp_path / "small.txt"
        small.write_bytes(b"hi")
        mock_confluence.post("/rest/api/content/123456/child/attachment").mock(
            return_value=httpx.Response(200, json={
                "results": [{
                    "id": "att-new",
                    "title": "small.txt",
                    "version": {
                        "number": 1,
                        "when": "2026-05-14T00:00:00.000Z",
                        "by": {"displayName": "u"},
                    },
                    "_links": {"download": "/x"},
                }]
            })
        )
        await tool_handlers["confluence_upload_attachment"](
            {"page_id": "123456", "file_path": str(small)}
        )
        post = next(c for c in mock_confluence.calls if c.request.method == "POST")
        assert post.request.headers["X-Atlassian-Token"] == "no-check"

    async def test_upload_writes_comment_into_multipart(
        self, tool_handlers, mock_confluence, tmp_path
    ):
        """Regression: `comment` was a dead arg — it appeared in the tool's
        text response but never made it onto the wire. This test inspects the
        multipart body to make sure the comment field really ships.
        """
        small = tmp_path / "small.txt"
        small.write_bytes(b"hi")
        mock_confluence.post("/rest/api/content/123456/child/attachment").mock(
            return_value=httpx.Response(200, json={
                "results": [{
                    "id": "att-new",
                    "title": "small.txt",
                    "version": {
                        "number": 1,
                        "when": "2026-05-14T00:00:00.000Z",
                        "by": {"displayName": "u"},
                    },
                    "_links": {"download": "/x"},
                }]
            })
        )

        await tool_handlers["confluence_upload_attachment"]({
            "page_id": "123456",
            "file_path": str(small),
            "comment": "release notes",
        })

        post = next(c for c in mock_confluence.calls if c.request.method == "POST")
        body = post.request.content
        # multipart bodies use form-data parts; the comment field shows up as
        # a `name="comment"` Content-Disposition with the value in the body.
        assert b'name="comment"' in body, (
            "comment must travel as a multipart form field — got body:\n"
            + body[:500].decode("latin1", errors="replace")
        )
        assert b"release notes" in body

    async def test_upload_without_comment_omits_field(
        self, tool_handlers, mock_confluence, tmp_path
    ):
        """No comment passed → no `name="comment"` part on the wire.

        Pairs with the regression test above: confirms the comment plumbing
        is opt-in, not always-on.
        """
        small = tmp_path / "small.txt"
        small.write_bytes(b"hi")
        mock_confluence.post("/rest/api/content/123456/child/attachment").mock(
            return_value=httpx.Response(200, json={
                "results": [{
                    "id": "att-new",
                    "title": "small.txt",
                    "version": {
                        "number": 1,
                        "when": "2026-05-14T00:00:00.000Z",
                        "by": {"displayName": "u"},
                    },
                    "_links": {"download": "/x"},
                }]
            })
        )

        await tool_handlers["confluence_upload_attachment"](
            {"page_id": "123456", "file_path": str(small)}
        )

        post = next(c for c in mock_confluence.calls if c.request.method == "POST")
        assert b'name="comment"' not in post.request.content
