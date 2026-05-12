"""Async HTTP client for Confluence Server REST API."""

import base64
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, quote

import httpx

from .config import Settings
from .errors import (
    ConfluenceAuthError,
    ConfluenceError,
    ConfluenceNotFoundError,
    ConfluencePermissionError,
    ConfluenceRateLimitError,
    ConfluenceSizeLimitError,
    ConfluenceValidationError,
    ConfluenceVersionConflictError,
)

# HTTP methods that are safe to auto-retry on 5xx. POST/PUT/DELETE are not on
# this list because a server that 5xx's *after* applying the write would cause
# duplicate creates or unintended re-applies.
IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS"}

logger = logging.getLogger(__name__)


class ConfluenceClient:
    """Async client for Confluence Server REST API."""

    def __init__(self, settings: Settings):
        self.settings = settings

        # Validate required settings
        if not settings.confluence_base_url:
            raise ValueError("CONFLUENCE_BASE_URL is required")
        if not settings.confluence_username:
            raise ValueError("CONFLUENCE_USERNAME is required")
        if not settings.confluence_password:
            raise ValueError("CONFLUENCE_PASSWORD is required")

        self.base_url = settings.confluence_base_url

        # Setup Basic Auth
        credentials = f"{settings.confluence_username}:{settings.confluence_password}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()

        self.headers = {
            "Authorization": f"Basic {encoded_credentials}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        # HTTP client will be set during lifespan
        self.http_client: Optional[httpx.AsyncClient] = None

    def set_http_client(self, client: httpx.AsyncClient) -> None:
        """Set the HTTP client instance."""
        self.http_client = client

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        files: Optional[Dict[str, Any]] = None,
        retry_on_5xx: bool = True,
    ) -> Dict[str, Any]:
        """Make an HTTP request to Confluence API."""
        if not self.http_client:
            raise RuntimeError("HTTP client not initialized")

        url = urljoin(self.base_url + "/", path.lstrip("/"))
        request_headers = self.headers.copy()
        if headers:
            request_headers.update(headers)

        # For file uploads, don't set Content-Type (httpx will set multipart)
        if files:
            request_headers.pop("Content-Type", None)
            request_headers["X-Atlassian-Token"] = "no-check"  # CSRF bypass for uploads

        logger.info(f"{method} {url} (params={params})")

        try:
            response = await self.http_client.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
                headers=request_headers,
                files=files,
            )

            logger.info(f"Response: {response.status_code}")

            # Retry on 5xx, but only for idempotent methods — retrying a POST
            # that the server might have already applied would create duplicates.
            if (
                response.status_code >= 500
                and retry_on_5xx
                and method.upper() in IDEMPOTENT_METHODS
            ):
                logger.warning(f"5xx error on {method}, retrying once: {response.status_code}")
                return await self._request(
                    method, path, params, json_data, headers, files, retry_on_5xx=False
                )

            # Handle error responses
            if response.status_code >= 400:
                await self._handle_error_response(response)

            # Handle successful responses
            if response.status_code == 204:  # No content
                return {}

            return response.json()

        except httpx.RequestError as e:
            logger.error(f"Request error: {e}")
            raise ConfluenceError(f"Request failed: {e}")

    async def _handle_error_response(self, response: httpx.Response) -> None:
        """Map an HTTP error response to a specific Confluence exception."""
        error_data: Dict[str, Any] = {}
        try:
            error_data = response.json() or {}
            message = error_data.get("message") or f"HTTP {response.status_code}"
        except Exception:
            message = f"HTTP {response.status_code}: {response.text[:200]}"

        code = response.status_code
        if code == 401:
            raise ConfluenceAuthError(message)
        if code == 403:
            raise ConfluencePermissionError(message)
        if code == 404:
            raise ConfluenceNotFoundError("Resource", message)
        if code == 400:
            raise ConfluenceValidationError("request", message)
        if code == 409:
            # Confluence usually doesn't put version numbers in the body for 409.
            # Surface whatever message the server gave; caller may fill in versions.
            raise ConfluenceVersionConflictError(message)
        if code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                retry_seconds = int(retry_after) if retry_after else None
            except ValueError:
                retry_seconds = None
            raise ConfluenceRateLimitError(message, retry_after_seconds=retry_seconds)
        raise ConfluenceError(message, code)

    # Search operations
    async def search_content(
        self,
        cql: str,
        limit: int = 25,
        start: int = 0,
        expand: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Search content using CQL (Confluence Query Language)."""
        params = {
            "cql": cql,
            "limit": min(limit, 100),  # Confluence limit
            "start": start,
        }
        if expand:
            params["expand"] = expand

        return await self._request("GET", "/rest/api/content/search", params=params)

    # Page operations
    async def get_content(
        self,
        content_id: str,
        expand: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get content by ID."""
        params = {}
        if expand:
            params["expand"] = expand

        return await self._request("GET", f"/rest/api/content/{content_id}", params=params)

    async def get_content_by_title(
        self,
        space_key: str,
        title: str,
        expand: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get content by space and title."""
        params = {
            "spaceKey": space_key,
            "title": title,
        }
        if expand:
            params["expand"] = expand

        response = await self._request("GET", "/rest/api/content", params=params)

        results = response.get("results", [])
        if not results:
            raise ConfluenceNotFoundError("Page", f"{space_key}:{title}")

        return results[0]

    async def create_content(self, content_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create new content (page or comment)."""
        return await self._request("POST", "/rest/api/content", json_data=content_data)

    async def update_content(self, content_id: str, content_data: Dict[str, Any]) -> Dict[str, Any]:
        """Update content with automatic version handling.

        There is a small race window between GET (to read current version) and
        PUT; on conflict we surface the server's error message verbatim along
        with the version numbers we *attempted*, so the caller can log enough
        context to diagnose.
        """
        current = await self.get_content(content_id, expand="version")
        current_version = current["version"]["number"]
        attempted = current_version + 1
        content_data["version"] = {"number": attempted}

        try:
            return await self._request(
                "PUT", f"/rest/api/content/{content_id}", json_data=content_data
            )
        except ConfluenceVersionConflictError as exc:
            # Preserve the real server message, just enrich with the version
            # numbers we used. Confluence rarely echoes them in the response body.
            raise ConfluenceVersionConflictError(
                f"{exc.message} (attempted version {attempted} based on observed {current_version})",
                current_version=current_version,
                provided_version=attempted,
            ) from exc

    async def delete_content(self, content_id: str) -> Dict[str, Any]:
        """Delete (trash) content."""
        return await self._request("DELETE", f"/rest/api/content/{content_id}")

    # Comment operations
    async def get_comments(
        self,
        page_id: str,
        expand: Optional[str] = None,
        limit: int = 25,
        start: int = 0,
    ) -> Dict[str, Any]:
        """Get comments for a page."""
        params = {
            "limit": min(limit, 100),
            "start": start,
        }
        if expand:
            params["expand"] = expand

        return await self._request(
            "GET",
            f"/rest/api/content/{page_id}/child/comment",
            params=params,
        )

    # Attachment operations
    async def get_attachments(
        self,
        page_id: str,
        limit: int = 25,
        start: int = 0,
    ) -> Dict[str, Any]:
        """Get attachments for a page."""
        params = {
            "limit": min(limit, 100),
            "start": start,
        }

        return await self._request(
            "GET",
            f"/rest/api/content/{page_id}/child/attachment",
            params=params,
        )

    async def download_attachment(self, page_id: str, filename: str) -> bytes:
        """Download attachment content.

        - `quote(filename, safe="")` so a filename containing `/` cannot pivot
          to a different REST path.
        - Inspects `Content-Length` before reading the body; refuses to stream
          past `settings.confluence_max_download_bytes`.
        """
        if not self.http_client:
            raise RuntimeError("HTTP client not initialized")

        encoded_filename = quote(filename, safe="")
        url = urljoin(
            self.base_url + "/",
            f"download/attachments/{quote(str(page_id), safe='')}/{encoded_filename}",
        )

        logger.info(f"Downloading attachment: {url}")

        limit = self.settings.confluence_max_download_bytes
        try:
            async with self.http_client.stream(
                "GET",
                url,
                headers={"Authorization": self.headers["Authorization"]},
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    await self._handle_error_response(response)

                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared = int(content_length)
                    except ValueError:
                        declared = -1
                    if declared > limit:
                        raise ConfluenceSizeLimitError(declared, limit)

                # Stream into a buffer; abort the moment we exceed the cap.
                buf = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(buf) + len(chunk) > limit:
                        raise ConfluenceSizeLimitError(len(buf) + len(chunk), limit)
                    buf.extend(chunk)
                return bytes(buf)
        except httpx.RequestError as e:
            logger.error(f"Download error: {e}")
            raise ConfluenceError(f"Download failed: {e}")

    async def upload_attachment(
        self,
        page_id: str,
        filename: str,
        content: bytes,
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload an attachment to a page.

        Rejects payloads larger than `confluence_max_upload_bytes` before
        touching the network, so an oversized file never gets buffered into
        the request body.
        """
        size = len(content)
        limit = self.settings.confluence_max_upload_bytes
        if size > limit:
            raise ConfluenceSizeLimitError(size, limit)

        files = {"file": (filename, content)}
        # X-Atlassian-Token: no-check is added in _request when `files` is set.
        return await self._request(
            "POST",
            f"/rest/api/content/{page_id}/child/attachment",
            files=files,
        )

    # Health check
    async def health_check(self) -> bool:
        """Check if Confluence is reachable."""
        try:
            await self._request("GET", "/rest/api/space", params={"limit": 1})
            return True
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False