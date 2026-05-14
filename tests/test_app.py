"""Tests for FastAPI application."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from confluence_mcp.app import create_app


class TestFastAPIApp:
    """Test FastAPI application.

    The TestClient is used *without* `with` on purpose: that keeps the lifespan
    from running so `app.state.confluence_client` stays unset and we can drive
    the /healthz branches deterministically without ever touching the network.
    """

    @pytest.fixture
    def app(self):
        return create_app()

    @pytest.fixture
    def client(self, app):
        return TestClient(app)

    def test_root_endpoint(self, client):
        response = client.get("/")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Confluence MCP Server"
        assert "endpoints" in data
        assert "/healthz" in data["endpoints"]["health"]

    def test_health_endpoint_returns_503_when_state_not_initialized(self, app, client):
        """Lifespan didn't run → no confluence_client on state → 503."""
        # Sanity check: state must not have a client (we deliberately skipped lifespan).
        assert not hasattr(app.state, "confluence_client")

        response = client.get("/healthz")
        assert response.status_code == 503
        assert response.json()["detail"] == "Service not initialized"

    def test_health_endpoint_does_not_leak_exception_details(self, app, client):
        """When health_check raises, the response detail must be a constant.

        Regression: a previous version returned f"Health check failed: {e}",
        which leaks httpx/Confluence error text — potentially URLs, credentials
        baked into URLs, or CAPTCHA-lock hints — to any caller of /healthz.
        """
        fake = AsyncMock()
        fake.health_check = AsyncMock(
            side_effect=RuntimeError("boom: https://user:pw@confluence/internal")
        )
        app.state.confluence_client = fake

        response = client.get("/healthz")

        assert response.status_code == 503
        body = response.text
        # The constant detail is exposed…
        assert response.json()["detail"] == "Confluence unreachable"
        # …and nothing from the underlying exception leaks through.
        assert "boom" not in body
        assert "user:pw" not in body
        assert "internal" not in body

    def test_health_endpoint_returns_503_when_health_check_returns_false(self, app, client):
        fake = AsyncMock()
        fake.health_check = AsyncMock(return_value=False)
        app.state.confluence_client = fake

        response = client.get("/healthz")
        assert response.status_code == 503
        assert response.json()["detail"] == "Confluence unreachable"

    def test_health_endpoint_returns_200_when_healthy(self, app, client):
        fake = AsyncMock()
        fake.health_check = AsyncMock(return_value=True)
        app.state.confluence_client = fake

        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy", "confluence": "connected"}

    def test_sse_route_registered(self, app):
        """The SSE GET endpoint must be on the app."""
        paths = {
            getattr(r, "path", None)
            for r in app.routes
            if getattr(r, "path", None) is not None
        }
        assert "/sse" in paths

    def test_messages_mount_registered(self, app):
        """The /messages/ Mount routes JSON-RPC POSTs to the SSE transport."""
        mounts = [r for r in app.routes if r.__class__.__name__ == "Mount"]
        assert any(getattr(m, "path", "").startswith("/messages") for m in mounts), (
            f"No /messages mount found in routes: {[type(r).__name__ for r in app.routes]}"
        )


@pytest.mark.integration
class TestIntegrationWithConfluence:
    """Integration tests that require a live Confluence instance.

    Skipped by default — set CONFLUENCE_INTEGRATION_TEST=1 to run.
    """

    @pytest.fixture
    def real_app(self):
        import os
        if not os.getenv("CONFLUENCE_INTEGRATION_TEST"):
            pytest.skip("Integration tests require CONFLUENCE_INTEGRATION_TEST=1")

        return create_app()

    @pytest.fixture
    def integration_client(self, real_app):
        # `with` so lifespan runs and the real Confluence client is built.
        with TestClient(real_app) as client:
            yield client

    def test_health_check_integration(self, integration_client):
        response = integration_client.get("/healthz")

        assert response.status_code in [200, 503]
        if response.status_code == 200:
            data = response.json()
            assert data["status"] == "healthy"
            assert data["confluence"] == "connected"
