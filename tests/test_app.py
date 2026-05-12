"""Tests for FastAPI application."""

import pytest
from fastapi.testclient import TestClient
import httpx

from confluence_mcp.app import create_app


class TestFastAPIApp:
    """Test FastAPI application."""

    @pytest.fixture
    def app(self):
        """Create FastAPI app for testing."""
        return create_app()

    @pytest.fixture
    def client(self, app):
        """Create test client."""
        return TestClient(app)

    def test_root_endpoint(self, client):
        """Test root endpoint."""
        response = client.get("/")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Confluence MCP Server"
        assert "endpoints" in data
        assert "/healthz" in data["endpoints"]["health"]

    @pytest.mark.asyncio
    async def test_health_endpoint_without_confluence(self, client):
        """Test health endpoint when Confluence client not initialized."""
        response = client.get("/healthz")

        # Should fail because confluence client is not initialized in test
        assert response.status_code == 503
        assert "Service not initialized" in response.json()["detail"]

    def test_sse_route_registered(self, app):
        """Inspect routes — the SSE GET endpoint must be on the app."""
        paths = {
            getattr(r, "path", None)
            for r in app.routes
            if getattr(r, "path", None) is not None
        }
        assert "/sse" in paths

    def test_messages_mount_registered(self, app):
        """The /messages/ Mount routes JSON-RPC POSTs to the SSE transport."""
        # A Mount exposes its path via .path (ending slash is stripped by Starlette
        # internally, but we registered "/messages/")
        mounts = [r for r in app.routes if r.__class__.__name__ == "Mount"]
        assert any(getattr(m, "path", "").startswith("/messages") for m in mounts), (
            f"No /messages mount found in routes: {[type(r).__name__ for r in app.routes]}"
        )


@pytest.mark.integration
class TestIntegrationWithConfluence:
    """Integration tests that require a live Confluence instance.

    These tests are skipped by default. Set CONFLUENCE_INTEGRATION_TEST=1
    environment variable to run them.
    """

    @pytest.fixture
    def real_app(self):
        """Create app with real settings for integration testing."""
        import os
        if not os.getenv("CONFLUENCE_INTEGRATION_TEST"):
            pytest.skip("Integration tests require CONFLUENCE_INTEGRATION_TEST=1")

        return create_app()

    @pytest.fixture
    def integration_client(self, real_app):
        """Create test client for integration testing."""
        return TestClient(real_app)

    def test_health_check_integration(self, integration_client):
        """Test health check against real Confluence instance."""
        # This would test against a real Confluence instance
        # The test is skipped unless CONFLUENCE_INTEGRATION_TEST is set
        response = integration_client.get("/healthz")

        # Should return 200 if Confluence is reachable, 503 if not
        assert response.status_code in [200, 503]

        if response.status_code == 200:
            data = response.json()
            assert data["status"] == "healthy"
            assert data["confluence"] == "connected"