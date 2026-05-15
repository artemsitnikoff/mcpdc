"""End-to-end check that /sse speaks the MCP protocol, not a stub.

Why this exists: TestClient route tests only confirm `/sse` exists. The
project has already burned once on a subagent-built stub that emitted
`{type: connected}` instead of the MCP `endpoint` event (CLAUDE.md, item 5).
Unit tests stayed green; real MCP clients couldn't connect.

This test boots the actual app on a real socket and reads the first SSE
frame coming out of `/sse`. The MCP SSE transport's contract is to emit:

    event: endpoint
    data: /messages/?session_id=<uuid>

If we see anything else (or a 5xx, or a hung connection), MCP clients
won't work — regardless of how many other tests pass.

We do NOT use the full `mcp.client.sse` client here. Driving the protocol
round-trip in-process is fragile (uvicorn lifespan + pytest-asyncio share
an event loop; in practice this hung for minutes on httpx ReadTimeout).
Reading the first frame is enough to detect the stub-handler class of
regressions.
"""

import asyncio
import socket

import httpx
import pytest
import uvicorn

from confluence_mcp.app import create_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _start_app(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://fake.confluence.test")
    monkeypatch.setenv("CONFLUENCE_USERNAME", "u")
    monkeypatch.setenv("CONFLUENCE_PASSWORD", "p")
    monkeypatch.setenv("CONFLUENCE_VERIFY_SSL", "false")

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
            async with client.stream("GET", f"http://127.0.0.1:{port}/sse") as r:
                assert r.status_code == 200, (
                    f"/sse must return 200, got {r.status_code}"
                )
                assert "text/event-stream" in r.headers.get("content-type", ""), (
                    f"/sse must be an SSE stream, got Content-Type={r.headers.get('content-type')!r}"
                )

                # Read enough bytes to cover the first SSE event, no more.
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
        # The MCP SSE transport names the first event `endpoint` and ships
        # the JSON-RPC POST URL in the data line.
        assert "event: endpoint" in first_event, (
            f"Expected `event: endpoint` as first SSE frame; got:\n{first_event!r}"
        )
        assert "/messages/" in first_event, (
            f"endpoint event must include the /messages/ URL; got:\n{first_event!r}"
        )
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_messages_endpoint_is_mounted(monkeypatch, tmp_path):
    """A POST to /messages/ must reach the SSE transport, not FastAPI's default 404.

    The transport will reject our made-up session_id with its own 404 (logged
    as "Could not find session for ID: ..."), and that's fine — we just need
    to prove the mount is wired. A FastAPI default `{"detail":"Not Found"}`
    body means `app.mount("/messages/", _messages_asgi)` is missing or
    misrouted.
    """
    server, task, port = await _start_app(monkeypatch, tmp_path)
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/messages/?session_id=00000000-0000-0000-0000-000000000000",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            )

        # FastAPI's default 404 ships `application/json` with `{"detail":"Not
        # Found"}`. The SSE transport's "unknown session" 404 ships a different
        # shape (text/plain, no `detail` key). Asserting on shape rather than
        # byte-equality keeps this robust against starlette tweaking spacing
        # in its default JSON serializer.
        ct = response.headers.get("content-type", "")
        # Guard `.json()` — a future regression could return JSON content-type
        # with an empty body, which would otherwise blow up the assert with a
        # JSONDecodeError instead of a meaningful test failure.
        detail = None
        if response.content and "application/json" in ct:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
        is_fastapi_default_404 = (
            response.status_code == 404
            and "application/json" in ct
            and detail == "Not Found"
        )
        assert not is_fastapi_default_404, (
            f"/messages/ returned FastAPI's default 404, so the mount is "
            f"missing. Status={response.status_code} content-type={ct!r} "
            f"body={response.text!r}"
        )
    finally:
        await _stop(server, task)
