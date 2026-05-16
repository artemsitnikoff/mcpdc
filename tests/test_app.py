"""Tests for FastAPI application — root, /healthz, route registration.

The TestClient is used *without* `with` on purpose: that keeps the lifespan
from running so `app.state.http_client` stays unset and we can drive the
/healthz branches deterministically without ever touching the network.
"""

import pytest
from fastapi.testclient import TestClient

from confluence_mcp.app import create_app


class TestFastAPIApp:
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
        # The root metadata advertises the auth model so MCP clients know to
        # supply Basic Auth headers.
        assert "Basic Auth" in data["auth"]

    def test_health_endpoint_returns_503_when_state_not_initialized(self, app, client):
        """Lifespan didn't run → no http_client on state → 503."""
        assert not hasattr(app.state, "http_client")
        response = client.get("/healthz")
        assert response.status_code == 503
        assert response.json()["detail"] == "Service not initialized"

    def test_health_endpoint_returns_200_when_lifespan_artifacts_present(
        self, app, client
    ):
        """When http_client + sse_transport are set, /healthz is a simple liveness probe.

        Crucially, it does NOT make any Confluence call: this server has no
        shared service account, so Confluence reachability is a per-user
        concern surfaced at SSE connect time.
        """
        app.state.http_client = object()  # sentinel — never used
        app.state.sse_transport = object()

        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_sse_route_registered(self, app):
        paths = {
            getattr(r, "path", None)
            for r in app.routes
            if getattr(r, "path", None) is not None
        }
        assert "/sse" in paths

    def test_messages_mount_registered(self, app):
        mounts = [r for r in app.routes if r.__class__.__name__ == "Mount"]
        assert any(getattr(m, "path", "").startswith("/messages") for m in mounts), (
            f"No /messages mount found in routes: {[type(r).__name__ for r in app.routes]}"
        )


@pytest.mark.integration
class TestIntegrationWithConfluence:
    """Integration tests against a live Confluence instance.

    Skipped by default — set CONFLUENCE_INTEGRATION_TEST=1 to run. With the
    Basic-Auth-pass-through model, /healthz no longer touches Confluence, so
    the only useful integration probe is /sse with real creds, which lives
    in its own (manual) workflow.
    """

    @pytest.fixture
    def real_app(self):
        import os
        if not os.getenv("CONFLUENCE_INTEGRATION_TEST"):
            pytest.skip("Integration tests require CONFLUENCE_INTEGRATION_TEST=1")
        return create_app()

    @pytest.fixture
    def integration_client(self, real_app):
        with TestClient(real_app) as client:
            yield client

    def test_healthz_is_alive(self, integration_client):
        response = integration_client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}
