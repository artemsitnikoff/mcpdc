"""End-to-end check that /sse speaks the MCP protocol, not a stub.

Why this exists: TestClient route tests only confirm `/sse` exists. The
project has already burned once on a subagent-built stub that emitted
`{type: connected}` instead of the MCP `endpoint` event (CLAUDE.md, item 5).
Unit tests stayed green; real MCP clients couldn't connect.

This test boots the actual app on a real socket and reads the first SSE
frame coming out of `/sse`. The MCP SSE transport's contract is to emit:

    event: endpoint
    data: /messages/?session_id=<uuid>

We do NOT use the full `mcp.client.sse` client here — driving the protocol
round-trip in-process is fragile (uvicorn lifespan + pytest-asyncio share an
event loop). Reading the first frame is enough to detect the stub-handler
class of regressions.

Auth: /sse now requires `Authorization: Basic` and validates against
Confluence via `validate_credentials` (which raises on auth/transport
failures, unlike the liberal `health_check`). To keep the test offline, we
monkeypatch `ConfluenceClient.validate_credentials` so any non-empty
credentials are accepted.
"""

import asyncio
import base64
import socket

import httpx
import pytest
import uvicorn

from confluence_mcp.app import create_app
from confluence_mcp.client import ConfluenceClient


def _basic(user: str, password: str) -> str:
    """Build a `Basic <base64>` header value.

    Kept as a helper so tests can be explicit about which user authenticated,
    instead of relying on a magic module-level constant.
    """
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _start_app(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://fake.confluence.test")
    monkeypatch.setenv("CONFLUENCE_VERIFY_SSL", "false")

    # Bypass the per-session Confluence probe. We're testing transport, not
    # auth integration — those tests live in test_auth.py. The probe used
    # by /sse is `validate_credentials` (returns None on success, raises on
    # failure), not `health_check`.
    async def _always_ok(self):
        return None

    monkeypatch.setattr(ConfluenceClient, "validate_credentials", _always_ok)

    port = _free_port()
    config = uvicorn.Config(
        create_app(),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    for _ in range(60):
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:
        server.should_exit = True
        await task
        raise RuntimeError("uvicorn did not start within 3s")

    return server, task, port


async def _stop(server, task):
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5)
    except asyncio.TimeoutError:
        task.cancel()


@pytest.mark.asyncio
async def test_sse_emits_mcp_endpoint_event(monkeypatch, tmp_path):
    """First SSE frame from /sse MUST be `event: endpoint` with a /messages/ URL.

    Anything else means the transport isn't really MCP, which is the exact
    bug we shipped a guardrail for in CLAUDE.md.
    """
    server, task, port = await _start_app(monkeypatch, tmp_path)
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            async with client.stream(
                "GET",
                f"http://127.0.0.1:{port}/sse",
                headers={"Authorization": _basic("u", "p")},
            ) as r:
                assert r.status_code == 200, (
                    f"/sse must return 200 for an authenticated request, got {r.status_code}"
                )
                assert "text/event-stream" in r.headers.get("content-type", ""), (
                    f"/sse must be an SSE stream, got Content-Type={r.headers.get('content-type')!r}"
                )

                buf = ""
                try:
                    async for chunk in r.aiter_text():
                        buf += chunk
                        if "\n\n" in buf:
                            break
                        if len(buf) > 4096:
                            break
                except (httpx.ReadTimeout, httpx.RemoteProtocolError):
                    pass

        first_event = buf.split("\n\n", 1)[0]
        assert "event: endpoint" in first_event, (
            f"Expected `event: endpoint` as first SSE frame; got:\n{first_event!r}"
        )
        assert "/messages/" in first_event, (
            f"endpoint event must include the /messages/ URL; got:\n{first_event!r}"
        )
    finally:
        await _stop(server, task)


# `test_messages_endpoint_is_mounted` used to live here. It probed /messages/
# with a fabricated session_id, which now short-circuits in `_messages_asgi`'s
# unknown-owner branch (404 from our own guard) and never reaches the SDK
# transport — so it stopped proving anything about the mount itself.
# The replacement lives in tests/test_auth.py as
# `test_messages_routes_to_transport_for_real_session`: it opens a real SSE
# session, captures the issued session_id from the endpoint event, and POSTs
# with that same session_id and matching credentials. Anything other than
# 202 from the SDK transport means the binding or mount broke.
