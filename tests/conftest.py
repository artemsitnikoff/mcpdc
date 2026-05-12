"""Pytest configuration and fixtures."""

import pytest
import respx
from httpx import AsyncClient

from confluence_mcp.client import ConfluenceClient
from confluence_mcp.config import Settings


@pytest.fixture
def confluence_settings():
    """Create test settings."""
    return Settings(
        confluence_base_url="https://test.confluence.com",
        confluence_username="testuser",
        confluence_password="testpass",
        confluence_verify_ssl=False,
        host="localhost",
        port=8000,
        log_level="DEBUG",
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