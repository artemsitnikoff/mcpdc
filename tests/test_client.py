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
        mock_confluence.get("/rest/api/space").mock(
            return_value=httpx.Response(200, json={"results": []})
        )

        result = await confluence_client.health_check()

        assert result is True

    async def test_health_check_failure(self, confluence_client, mock_confluence):
        """Test failed health check."""
        mock_confluence.get("/rest/api/space").mock(
            return_value=httpx.Response(500, text="Server Error")
        )

        result = await confluence_client.health_check()

        assert result is False

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