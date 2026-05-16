"""Tests for Confluence client."""

import pytest
import httpx

from confluence_mcp.errors import (
    ConfluenceNotFoundError,
    ConfluencePermissionError,
    ConfluenceVersionConflictError,
)


class TestConfluenceClient:
    """Test Confluence client functionality."""

    async def test_search_content(self, confluence_client, mock_confluence):
        """Test content search."""
        # Mock successful search response
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
                    }
                ],
                "totalSize": 1,
            })
        )

        result = await confluence_client.search_content("space = TEST")

        assert result["totalSize"] == 1
        assert len(result["results"]) == 1
        assert result["results"][0]["title"] == "Test Page"

    async def test_get_content_by_id(self, confluence_client, mock_confluence):
        """Test getting content by ID."""
        mock_confluence.get("/rest/api/content/123456").mock(
            return_value=httpx.Response(200, json={
                "id": "123456",
                "title": "Test Page",
                "type": "page",
                "body": {
                    "storage": {
                        "value": "<p>Test content</p>",
                        "representation": "storage",
                    }
                },
                "version": {"number": 1},
            })
        )

        result = await confluence_client.get_content("123456")

        assert result["id"] == "123456"
        assert result["title"] == "Test Page"

    async def test_get_content_by_title(self, confluence_client, mock_confluence):
        """Test getting content by space and title."""
        mock_confluence.get("/rest/api/content").mock(
            return_value=httpx.Response(200, json={
                "results": [
                    {
                        "id": "123456",
                        "title": "Test Page",
                        "type": "page",
                    }
                ]
            })
        )

        result = await confluence_client.get_content_by_title("TEST", "Test Page")

        assert result["id"] == "123456"
        assert result["title"] == "Test Page"

    async def test_get_content_not_found(self, confluence_client, mock_confluence):
        """Test content not found error."""
        mock_confluence.get("/rest/api/content").mock(
            return_value=httpx.Response(200, json={"results": []})
        )

        with pytest.raises(ConfluenceNotFoundError):
            await confluence_client.get_content_by_title("TEST", "Non-existent Page")

    async def test_create_content(self, confluence_client, mock_confluence):
        """Test creating content."""
        mock_confluence.post("/rest/api/content").mock(
            return_value=httpx.Response(200, json={
                "id": "789012",
                "title": "New Page",
                "type": "page",
                "space": {"key": "TEST"},
                "version": {"number": 1, "when": "2024-01-01T00:00:00.000Z"},
                "_links": {"webui": "/spaces/TEST/pages/789012"},
            })
        )

        content_data = {
            "type": "page",
            "title": "New Page",
            "space": {"key": "TEST"},
            "body": {"storage": {"value": "<p>Content</p>", "representation": "storage"}},
        }

        result = await confluence_client.create_content(content_data)

        assert result["id"] == "789012"
        assert result["title"] == "New Page"

    async def test_update_content_with_version_handling(self, confluence_client, mock_confluence):
        """Test updating content with automatic version handling."""
        # Mock getting current content for version
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
                "version": {"number": 6, "when": "2024-01-01T01:00:00.000Z"},
                "_links": {"webui": "/spaces/TEST/pages/123456"},
            })
        )

        content_data = {
            "id": "123456",
            "type": "page",
            "title": "Updated Page",
            "body": {"storage": {"value": "<p>Updated content</p>", "representation": "storage"}},
        }

        result = await confluence_client.update_content("123456", content_data)

        assert result["version"]["number"] == 6
        assert result["title"] == "Updated Page"

        # Verify the PUT request included the correct version
        put_calls = [call for call in mock_confluence.calls if call.request.method == "PUT"]
        assert len(put_calls) == 1
        put_data = put_calls[0].request.content
        assert b'"version":{"number":6}' in put_data

    async def test_delete_content(self, confluence_client, mock_confluence):
        """Test deleting content."""
        mock_confluence.delete("/rest/api/content/123456").mock(
            return_value=httpx.Response(204)
        )

        result = await confluence_client.delete_content("123456")

        assert result == {}

    async def test_upload_attachment_with_csrf_header(self, confluence_client, mock_confluence):
        """Test uploading attachment includes required CSRF header."""
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
            "123456", "test.txt", b"test content", "Test comment"
        )

        assert "results" in result
        assert result["results"][0]["title"] == "test.txt"

        # Verify the CSRF header was included
        post_calls = [call for call in mock_confluence.calls if call.request.method == "POST"]
        assert len(post_calls) == 1
        post_request = post_calls[0].request
        assert post_request.headers["X-Atlassian-Token"] == "no-check"

    async def test_health_check_success(self, confluence_client, mock_confluence):
        """Test successful health check."""
        mock_confluence.get("/rest/api/user/current").mock(
            return_value=httpx.Response(200, json={"username": "testuser"})
        )

        result = await confluence_client.health_check()

        assert result is True

    async def test_health_check_failure(self, confluence_client, mock_confluence):
        """Test failed health check."""
        mock_confluence.get("/rest/api/user/current").mock(
            return_value=httpx.Response(500, text="Server Error")
        )

        result = await confluence_client.health_check()

        assert result is False

    async def test_validate_credentials_success(self, confluence_client, mock_confluence):
        """validate_credentials returns None on 200."""
        mock_confluence.get("/rest/api/user/current").mock(
            return_value=httpx.Response(200, json={"username": "testuser"})
        )
        # No exception, returns None.
        result = await confluence_client.validate_credentials()
        assert result is None

    async def test_validate_credentials_propagates_auth_error(
        self, confluence_client, mock_confluence
    ):
        """validate_credentials surfaces 401 as ConfluenceAuthError, not bool."""
        from confluence_mcp.errors import ConfluenceAuthError

        mock_confluence.get("/rest/api/user/current").mock(
            return_value=httpx.Response(401, json={"message": "bad creds"})
        )
        with pytest.raises(ConfluenceAuthError):
            await confluence_client.validate_credentials()

    async def test_validate_credentials_propagates_transport_error(
        self, confluence_client, mock_confluence
    ):
        """validate_credentials surfaces 5xx as ConfluenceError, distinct from auth and permission."""
        from confluence_mcp.errors import (
            ConfluenceAuthError,
            ConfluenceError,
            ConfluencePermissionError,
        )

        mock_confluence.get("/rest/api/user/current").mock(
            return_value=httpx.Response(503, text="upstream down")
        )
        with pytest.raises(ConfluenceError) as exc_info:
            await confluence_client.validate_credentials()
        # Must NOT be mis-typed as either of the subclasses — that's how
        # /sse distinguishes 401 vs 403 vs 502 in the response to the
        # MCP client.
        assert not isinstance(exc_info.value, ConfluenceAuthError)
        assert not isinstance(exc_info.value, ConfluencePermissionError)

    async def test_validate_credentials_propagates_permission_error(
        self, confluence_client, mock_confluence
    ):
        """validate_credentials surfaces 403 as ConfluencePermissionError.

        Regression: `ConfluencePermissionError` is a `ConfluenceError`
        subclass; if /sse's exception handler doesn't catch it before the
        generic branch, a "no read access" answer becomes a 502, sending
        users to chase outages instead of asking for permission.
        """
        from confluence_mcp.errors import ConfluencePermissionError

        mock_confluence.get("/rest/api/user/current").mock(
            return_value=httpx.Response(403, json={"message": "no read access"})
        )
        with pytest.raises(ConfluencePermissionError):
            await confluence_client.validate_credentials()

    async def test_request_maps_html_response_to_auth_error(
        self, confluence_client, mock_confluence
    ):
        """Confluence returns HTML 200 when an account is CAPTCHA-locked.

        `_request` must convert the resulting JSONDecodeError into a
        ConfluenceAuthError, so callers can surface "log in via UI" instead
        of a bare parse error.
        """
        from confluence_mcp.errors import ConfluenceAuthError

        mock_confluence.get("/rest/api/user/current").mock(
            return_value=httpx.Response(
                200,
                content=b"<html><body>Please log in</body></html>",
                headers={"content-type": "text/html; charset=utf-8"},
            )
        )
        with pytest.raises(ConfluenceAuthError) as exc_info:
            await confluence_client.validate_credentials()
        assert "CAPTCHA" in exc_info.value.message

    async def test_request_maps_xhtml_response_to_auth_error(
        self, confluence_client, mock_confluence
    ):
        """Same CAPTCHA mapping for `application/xhtml+xml`.

        Atlassian server pages historically use XHTML; relying purely on
        `text/html` would inverse-map XHTML CAPTCHA pages into a 502
        "unreachable" error at the /sse layer (same category bug as the
        403→502 inversion we fixed earlier).
        """
        from confluence_mcp.errors import ConfluenceAuthError

        mock_confluence.get("/rest/api/user/current").mock(
            return_value=httpx.Response(
                200,
                content=b"<html xmlns='http://www.w3.org/1999/xhtml'><body/></html>",
                headers={"content-type": "application/xhtml+xml; charset=utf-8"},
            )
        )
        with pytest.raises(ConfluenceAuthError) as exc_info:
            await confluence_client.validate_credentials()
        assert "CAPTCHA" in exc_info.value.message

    async def test_request_maps_other_non_json_to_transport_error(
        self, confluence_client, mock_confluence
    ):
        """Non-HTML, non-JSON responses get a plain ConfluenceError.

        Guards against the reverse mistake: lumping every non-JSON answer
        under CAPTCHA tells users to "log in via UI" when the real cause
        is a misbehaving proxy or upstream returning text/plain.
        """
        from confluence_mcp.errors import ConfluenceAuthError, ConfluenceError

        mock_confluence.get("/rest/api/user/current").mock(
            return_value=httpx.Response(
                200,
                content=b"OK",
                headers={"content-type": "text/plain"},
            )
        )
        with pytest.raises(ConfluenceError) as exc_info:
            await confluence_client.validate_credentials()
        # Not the auth-error subclass — must surface as a transport-like
        # error so /sse responds with 502, not 401.
        assert not isinstance(exc_info.value, ConfluenceAuthError)
        assert "text/plain" in exc_info.value.message

    async def test_error_handling_404(self, confluence_client, mock_confluence):
        """Test 404 error handling."""
        mock_confluence.get("/rest/api/content/nonexistent").mock(
            return_value=httpx.Response(404, json={"message": "Content not found"})
        )

        with pytest.raises(ConfluenceNotFoundError) as exc_info:
            await confluence_client.get_content("nonexistent")

        assert "Content not found" in str(exc_info.value)

    async def test_error_handling_403(self, confluence_client, mock_confluence):
        """Test 403 permission error handling."""
        mock_confluence.get("/rest/api/content/forbidden").mock(
            return_value=httpx.Response(403, json={"message": "Access denied"})
        )

        with pytest.raises(ConfluencePermissionError):
            await confluence_client.get_content("forbidden")

    async def test_error_handling_409_version_conflict(self, confluence_client, mock_confluence):
        """Test 409 version conflict error handling."""
        # Mock getting current content
        mock_confluence.get("/rest/api/content/123456").mock(
            return_value=httpx.Response(200, json={
                "id": "123456",
                "version": {"number": 5}
            })
        )

        # Mock version conflict on update
        mock_confluence.put("/rest/api/content/123456").mock(
            return_value=httpx.Response(409, json={"message": "Version conflict"})
        )

        with pytest.raises(ConfluenceVersionConflictError):
            await confluence_client.update_content("123456", {"title": "Test"})

    async def test_retry_on_5xx_error(self, confluence_client, mock_confluence):
        """Test retry logic on 5xx errors."""
        # First request fails with 500, second succeeds
        mock_confluence.get("/rest/api/content/retry-test").mock(
            side_effect=[
                httpx.Response(500, text="Server Error"),
                httpx.Response(200, json={"id": "retry-test", "title": "Success"}),
            ]
        )

        result = await confluence_client.get_content("retry-test")

        assert result["title"] == "Success"
        # Verify two requests were made
        get_calls = [call for call in mock_confluence.calls if call.request.method == "GET"]
        assert len(get_calls) == 2