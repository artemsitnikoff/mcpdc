"""FastAPI application factory for Confluence MCP server."""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
from fastapi import FastAPI, HTTPException, Request
from mcp.server.sse import SseServerTransport
from starlette.routing import Mount

from .client import ConfluenceClient
from .config import get_settings
from .mcp_server import create_mcp_server

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Starting Confluence MCP server...")

    settings = get_settings()

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        verify=settings.confluence_verify_ssl,
    )
    confluence_client = ConfluenceClient(settings)
    confluence_client.set_http_client(http_client)
    mcp_server = create_mcp_server(confluence_client)

    # Per-app SSE transport — keeping it per-instance means multiple create_app()
    # calls (notably in tests) don't share an in-memory session table.
    sse_transport = SseServerTransport("/messages/")

    app.state.http_client = http_client
    app.state.confluence_client = confluence_client
    app.state.mcp_server = mcp_server
    app.state.sse_transport = sse_transport

    logger.info("Confluence MCP server started successfully")

    yield

    logger.info("Shutting down Confluence MCP server...")
    await http_client.aclose()
    logger.info("Confluence MCP server stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Confluence MCP Server",
        description="Model Context Protocol server for Atlassian Confluence Server 7.4.6",
        version="0.1.0",
        lifespan=lifespan,
    )

    # TODO: CORS / rate limiting / auth — out of scope for MVP.

    @app.get("/healthz")
    async def health_check():
        confluence_client = getattr(app.state, "confluence_client", None)
        if not confluence_client:
            raise HTTPException(status_code=503, detail="Service not initialized")
        try:
            is_healthy = await confluence_client.health_check()
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            raise HTTPException(status_code=503, detail=f"Health check failed: {e}")
        if not is_healthy:
            raise HTTPException(status_code=503, detail="Confluence unreachable")
        return {"status": "healthy", "confluence": "connected"}

    @app.get("/")
    async def root():
        try:
            confluence_url = get_settings().confluence_base_url
        except Exception:
            confluence_url = "Not configured"
        return {
            "name": "Confluence MCP Server",
            "version": "0.1.0",
            "description": "Model Context Protocol server for Atlassian Confluence Server 7.4.6",
            "endpoints": {
                "health": "/healthz",
                "mcp_sse": "/sse",
                "mcp_messages": "/messages/",
            },
            "confluence_url": confluence_url,
        }

    @app.get("/sse")
    async def sse_endpoint(request: Request):
        sse_transport: SseServerTransport | None = getattr(app.state, "sse_transport", None)
        mcp_server = getattr(app.state, "mcp_server", None)
        if not sse_transport or not mcp_server:
            raise HTTPException(status_code=503, detail="MCP server not initialized")
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            await mcp_server.run(
                read_stream,
                write_stream,
                mcp_server.create_initialization_options(),
            )

    async def _messages_asgi(scope, receive, send):
        """ASGI sub-app for the /messages/ mount; defers to the per-app transport."""
        sse_transport = getattr(app.state, "sse_transport", None)
        if sse_transport is None:
            from starlette.responses import JSONResponse
            response = JSONResponse({"error": "MCP server not initialized"}, status_code=503)
            await response(scope, receive, send)
            return
        await sse_transport.handle_post_message(scope, receive, send)

    app.mount("/messages/", _messages_asgi)

    return app
