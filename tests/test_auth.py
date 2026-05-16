"""Tests for the Basic Auth pass-through model.

Two layers:
1. Pure-function tests of `_parse_basic_auth` — fast, no fixtures, covers
   every "bad header" branch (missing, wrong scheme, bad base64, missing
   colon, empty username/password).
2. Live HTTP tests against a real uvicorn instance for behaviour that only
   happens at the transport layer (`/sse` rejecting unauthenticated GETs,
   `/messages/` rejecting unauthenticated POSTs, and the contextvar carrying
   the *right* per-session client even when two clients connect concurrently).

The contextvar isolation test is the load-bearing one: without it, an
implementation that built one shared `ConfluenceClient` from the first
caller's credentials and reused it for every later session would pass every
other test in this file. The shared-credentials regression is exactly the
class of bug this whole refactor is supposed to prevent.
"""

import asyncio
import base64
import logging
import socket

import httpx
import pytest
import uvicorn
from fastapi import HTTPException

from confluence_mcp.app import _parse_basic_auth, create_app
from confluence_mcp.client import ConfluenceClient
from confluence_mcp.session import current_confluence_client


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# _parse_basic_auth — unit tests
# ---------------------------------------------------------------------------

class TestParseBasicAuth:
    def test_valid_header_returns_credentials(self):
        encoded = base64.b64encode(b"alice:secret").decode()
        u, p = _parse_basic_auth(f"Basic {encoded}")
        assert (u, p) == ("alice", "secret")

    def test_password_with_colon_is_preserved(self):
        # Confluence passwords can contain colons; split only on the first.
        encoded = base64.b64encode(b"alice:s:e:c:r:e:t").decode()
        u, p = _parse_basic_auth(f"Basic {encoded}")
        assert u == "alice"
        assert p == "s:e:c:r:e:t"

    @pytest.mark.parametrize(
        "header",
        [
            None,
            "",
            "Bearer some-token",
            "Basic",          # no payload at all
            "Digest xxx",
            "basic " + base64.b64encode(b"u:p").decode(),  # wrong case
        ],
    )
    def test_missing_or_wrong_scheme_raises_401(self, header):
        with pytest.raises(HTTPException) as exc:
            _parse_basic_auth(header)
        assert exc.value.status_code == 401
        assert exc.value.headers["WWW-Authenticate"].startswith("Basic")

    def test_invalid_base64_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            _parse_basic_auth("Basic !!!not-base64!!!")
        assert exc.value.status_code == 401

    def test_missing_colon_raises_401(self):
        encoded = base64.b64encode(b"no-colon-here").decode()
        with pytest.raises(HTTPException) as exc:
            _parse_basic_auth(f"Basic {encoded}")
        assert exc.value.status_code == 401

    def test_empty_username_raises_401(self):
        encoded = base64.b64encode(b":only-password").decode()
        with pytest.raises(HTTPException) as exc:
            _parse_basic_auth(f"Basic {encoded}")
        assert exc.value.status_code == 401

    def test_empty_password_raises_401(self):
        encoded = base64.b64encode(b"only-username:").decode()
        with pytest.raises(HTTPException) as exc:
            _parse_basic_auth(f"Basic {encoded}")
        assert exc.value.status_code == 401


# ---------------------------------------------------------------------------
# Live-server HTTP tests
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _start_app(monkeypatch, tmp_path, *, validate=None):
    """Boot the app and let the caller decide how the auth probe behaves.

    The /sse handler invokes `client.validate_credentials()` — pass a coroutine
    that either returns None (auth ok), raises `ConfluenceAuthError` (bad
    creds), or raises `ConfluenceError` (transport failure).

    Returns (server, task, port, app). `app` lets tests assert on internal
    state like `app.state.session_owners` directly — necessary for the
    substance check that the URL-side session_id matches our storage key.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://fake.confluence.test")
    monkeypatch.setenv("CONFLUENCE_VERIFY_SSL", "false")

    if validate is not None:
        monkeypatch.setattr(ConfluenceClient, "validate_credentials", validate)

    port = _free_port()
    app = create_app()
    config = uvicorn.Config(
        app,
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

    return server, task, port, app


async def _stop(server, task):
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5)
    except asyncio.TimeoutError:
        task.cancel()


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


async def _open_sse_and_capture(
    port: int, user: str, password: str, *, release: asyncio.Event
):
    """Open /sse, capture session_id from the endpoint event, keep the
    stream open until `release` is set.

    Returns (session_id, hold_task). The caller MUST `release.set()` and
    `await hold_task` (ignoring CancelledError/HTTPError) once it's done.

    Holding the SSE stream open matters: closing it removes the session
    from `app.state.session_owners`, so any /messages/ POST afterwards
    exercises the wrong code path (404 from the unknown-session branch
    instead of the test's intended branch).
    """
    session_id_future: asyncio.Future = asyncio.get_event_loop().create_future()

    async def _hold():
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            try:
                async with client.stream(
                    "GET",
                    f"http://127.0.0.1:{port}/sse",
                    headers={"Authorization": _basic(user, password)},
                ) as r:
                    assert r.status_code == 200
                    buf = ""
                    async for chunk in r.aiter_text():
                        buf += chunk
                        if not session_id_future.done() and "session_id=" in buf:
                            for line in buf.splitlines():
                                if line.startswith("data:") and "session_id=" in line:
                                    sid = line.split("session_id=", 1)[1].strip()
                                    session_id_future.set_result(sid)
                                    break
                        # Hard cap on buffer growth — if `session_id=`
                        # never appears, fall through to wait_for timeout
                        # rather than letting buf consume unbounded memory.
                        if len(buf) > 4096:
                            break
                        if release.is_set():
                            break
            except (httpx.HTTPError, asyncio.CancelledError) as exc:
                if not session_id_future.done():
                    session_id_future.set_exception(exc)
                else:
                    # Stream went down AFTER we already got session_id —
                    # log loudly so flaky-test debugging has a starting
                    # point instead of a silent test failure later.
                    logger.warning(
                        "_hold caught %s after session_id_future resolved: %s",
                        type(exc).__name__, exc,
                    )
                if isinstance(exc, asyncio.CancelledError):
                    raise

    hold_task = asyncio.create_task(_hold())
    session_id = await asyncio.wait_for(session_id_future, timeout=10)
    return session_id, hold_task


async def _release_hold(release: asyncio.Event, hold_task: asyncio.Task):
    release.set()
    hold_task.cancel()
    try:
        await hold_task
    except (asyncio.CancelledError, httpx.HTTPError):
        pass


@pytest.mark.asyncio
async def test_sse_rejects_request_without_authorization(monkeypatch, tmp_path):
    server, task, port, app = await _start_app(monkeypatch, tmp_path)
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(f"http://127.0.0.1:{port}/sse")
        assert response.status_code == 401
        assert response.headers.get("www-authenticate", "").startswith("Basic")
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_sse_rejects_credentials_confluence_refuses(monkeypatch, tmp_path):
    """When `validate_credentials` raises ConfluenceAuthError, /sse → 401."""
    from confluence_mcp.errors import ConfluenceAuthError

    async def _refuse(self):
        raise ConfluenceAuthError("nope")

    server, task, port, app = await _start_app(monkeypatch, tmp_path, validate=_refuse)
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(
                f"http://127.0.0.1:{port}/sse",
                headers={"Authorization": _basic("alice", "wrong")},
            )
        assert response.status_code == 401
        assert "text/event-stream" not in response.headers.get("content-type", "")
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_sse_returns_502_when_confluence_unreachable(monkeypatch, tmp_path):
    """When `validate_credentials` raises ConfluenceError, /sse → 502.

    Critical: users must be able to tell a creds problem (401) from an
    infrastructure problem (502). Lumping both under 401 used to send them
    on wild password-reset hunts during outages.
    """
    from confluence_mcp.errors import ConfluenceError

    async def _outage(self):
        # Mirror what `_request` actually raises on `httpx.RequestError`:
        # `ConfluenceError(f"Request failed: {e}")` with no status_code,
        # so status_code is None.
        raise ConfluenceError("Request failed: connection refused")

    server, task, port, app = await _start_app(monkeypatch, tmp_path, validate=_outage)
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(
                f"http://127.0.0.1:{port}/sse",
                headers={"Authorization": _basic("alice", "secret")},
            )
        assert response.status_code == 502
        # And specifically NOT 401 — that's the bug we're guarding against.
        assert response.json().get("detail") == "Confluence unreachable"
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_messages_rejects_cross_user_post(monkeypatch, tmp_path):
    """Session hijacking guard: POST /messages/ with creds of user B but
    session_id owned by user A must be refused with 403.

    The scenario:
    - Alice opens /sse with her creds → session_id_A registered to "alice".
    - Bob obtains session_id_A (leaked log, screenshot, proxy access record).
    - Bob POSTs to /messages/?session_id=session_id_A with HIS own valid
      Basic Auth. Before the guard, Bob's payload would have entered Alice's
      session and executed as Alice (because the contextvar in Alice's task
      held Alice's ConfluenceClient).
    """

    async def _ok(self):
        return None

    server, task, port, app = await _start_app(monkeypatch, tmp_path, validate=_ok)
    release = asyncio.Event()
    try:
        session_id, hold_task = await _open_sse_and_capture(
            port, "alice", "pw-a", release=release
        )
        try:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as bob_client:
                # Bob's POST hits Alice's still-open session. Without the
                # guard this would execute on Alice's behalf via her
                # contextvar-bound ConfluenceClient.
                response = await bob_client.post(
                    f"http://127.0.0.1:{port}/messages/?session_id={session_id}",
                    json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                    headers={"Authorization": _basic("bob", "pw-b")},
                )

            assert response.status_code == 403, (
                f"Cross-user POST must be rejected with 403, got "
                f"{response.status_code} body={response.text!r}"
            )
            assert "Session does not belong" in response.json().get("detail", "")
        finally:
            await _release_hold(release, hold_task)
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_sse_returns_403_for_account_without_read_access(monkeypatch, tmp_path):
    """`ConfluencePermissionError` from `validate_credentials` → 403, not 502.

    Regression: `ConfluencePermissionError` subclasses `ConfluenceError`. If
    `_build_session_client` doesn't catch it explicitly before the generic
    `ConfluenceError` handler, a legitimate "no read access" answer gets
    surfaced as "Confluence unreachable" — exactly the kind of inversion
    that sends users on wild infrastructure-debugging hunts.
    """
    from confluence_mcp.errors import ConfluencePermissionError

    async def _no_access(self):
        raise ConfluencePermissionError("account lacks read access")

    server, task, port, app = await _start_app(
        monkeypatch, tmp_path, validate=_no_access
    )
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(
                f"http://127.0.0.1:{port}/sse",
                headers={"Authorization": _basic("alice", "secret")},
            )
        assert response.status_code == 403, (
            f"Expected 403, got {response.status_code} body={response.text!r}"
        )
        # And specifically NOT 502 — that's the bug.
        assert "Confluence unreachable" not in response.text
        assert "no read access" in response.json().get("detail", "")
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_messages_routes_to_transport_for_real_session(monkeypatch, tmp_path):
    """End-to-end: URL-side session_id from /sse matches our storage key.

    This is the load-bearing substance test for the diff-trick used in
    `/sse` to discover the SDK-assigned session_id. It checks three
    things in one round-trip:

    1. The string the SDK shipped to the client in the `endpoint` event
       (the value of `?session_id=` in the URL) is a key in
       `app.state.session_owners`. If the SDK ever changes the URL
       encoding from `.hex` (32 chars, no dashes) to `str(uuid)` with
       dashes, this assertion fires immediately — without it the binding
       silently breaks and every POST 404s.
    2. That key is bound to the user who actually opened the SSE
       connection ("alice"), not somebody else.
    3. A POST as that same user with that same session_id reaches the
       SDK transport, which replies 202 — i.e. the mount/binding is
       still wired end-to-end.
    """

    async def _ok(self):
        return None

    server, task, port, app = await _start_app(monkeypatch, tmp_path, validate=_ok)
    release = asyncio.Event()
    try:
        session_id, hold_task = await _open_sse_and_capture(
            port, "alice", "pw-a", release=release
        )
        try:
            # Substance bind check: the string we extracted from the
            # endpoint event MUST be present in app.state.session_owners.
            # Hardcoded "alice" because that's who opened the stream.
            owners = dict(app.state.session_owners)
            assert session_id in owners, (
                f"session_id from endpoint event {session_id!r} not found in "
                f"app.state.session_owners (keys={list(owners)}). If the SDK "
                f"changed the URL encoding of session_id, our diff-trick still "
                f"records the old form and POSTs will never match."
            )
            assert owners[session_id] == "alice", (
                f"session {session_id!r} bound to {owners[session_id]!r}, "
                f"expected 'alice'"
            )

            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                # A valid MCP initialize payload — the transport hands this
                # to mcp_server.run; we don't await its semantic reply here.
                response = await client.post(
                    f"http://127.0.0.1:{port}/messages/?session_id={session_id}",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "0"},
                        },
                    },
                    headers={"Authorization": _basic("alice", "pw-a")},
                )

            assert response.status_code == 202, (
                f"Transport should accept POST and return 202; got "
                f"{response.status_code} body={response.text!r}. "
                f"Anything else means the mount/binding broke."
            )
        finally:
            await _release_hold(release, hold_task)
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_messages_with_unknown_session_id_returns_404(monkeypatch, tmp_path):
    """The session_owners binding refuses unknown session_ids with 404.

    Fail-closed guard: a session_id that was never registered (closed,
    fabricated, or stale) is rejected at our layer with a friendly 404,
    rather than being handed to the SDK transport where it could race
    with stream cleanup and surface as an opaque 500/ClosedResourceError.

    Note: this is NOT a coupling detector for the SDK private-attr
    contract — a hypothetical SDK switch from `.hex` to `str(uuid)` with
    dashes would still produce a 404 here (32 zeros never match anything
    either way). The substance bind test
    `test_messages_routes_to_transport_for_real_session` is the one that
    would catch such a regression.
    """

    async def _ok(self):
        return None

    server, task, port, app = await _start_app(monkeypatch, tmp_path, validate=_ok)
    release = asyncio.Event()
    try:
        # Open a real session so the app has at least one registered
        # session_id — guards against the "owners dict empty therefore
        # everything 404s trivially" failure mode.
        _real_session_id, hold_task = await _open_sse_and_capture(
            port, "alice", "pw-a", release=release
        )
        try:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                response = await client.post(
                    f"http://127.0.0.1:{port}/messages/?session_id=00000000000000000000000000000000",
                    json={"jsonrpc": "2.0", "method": "ping", "id": 1},
                    headers={"Authorization": _basic("alice", "pw-a")},
                )
            assert response.status_code == 404
            assert "Could not find session" in response.json().get("detail", "")
        finally:
            await _release_hold(release, hold_task)
    finally:
        await _stop(server, task)


@pytest.mark.asyncio
async def test_messages_rejects_request_without_authorization(monkeypatch, tmp_path):
    async def _ok(self):
        return None

    server, task, port, app = await _start_app(monkeypatch, tmp_path, validate=_ok)
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/messages/?session_id=00000000-0000-0000-0000-000000000000",
                json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            )
        assert response.status_code == 401
        assert response.headers.get("www-authenticate", "").startswith("Basic")
    finally:
        await _stop(server, task)


# ---------------------------------------------------------------------------
# Contextvar isolation
# ---------------------------------------------------------------------------

class TestContextvarIsolation:
    """The contextvar must carry per-session state, not leak between sessions."""

    def test_lazy_proxy_raises_without_active_session(self):
        """Outside any /sse context, the proxy should refuse rather than
        silently fall through to None/AttributeError.
        """
        from confluence_mcp.session import LazyConfluenceClient

        proxy = LazyConfluenceClient()
        with pytest.raises(LookupError):
            _ = proxy.headers  # would normally route to client.headers

    def test_contextvar_set_resets_cleanly(self, confluence_client):
        """Setting and resetting the contextvar must not leak across scopes."""
        with pytest.raises(LookupError):
            current_confluence_client.get()

        token = current_confluence_client.set(confluence_client)
        try:
            assert current_confluence_client.get() is confluence_client
        finally:
            current_confluence_client.reset(token)

        with pytest.raises(LookupError):
            current_confluence_client.get()

    @pytest.mark.asyncio
    async def test_concurrent_tasks_see_distinct_clients(
        self, confluence_settings, http_client
    ):
        """Two concurrent asyncio tasks, each with their own contextvar value,
        must see their own ConfluenceClient — never each other's.

        This is the regression guard for the "shared service account" bug:
        if the implementation accidentally captured one client globally, both
        tasks would see the same `headers["Authorization"]` here.
        """
        alice = ConfluenceClient(confluence_settings, "alice", "pw-a")
        alice.set_http_client(http_client)
        bob = ConfluenceClient(confluence_settings, "bob", "pw-b")
        bob.set_http_client(http_client)

        seen = {}
        gate = asyncio.Event()

        async def run_as(label, client):
            token = current_confluence_client.set(client)
            try:
                # Wait so both tasks are concurrently inside this critical
                # section — if there were a single shared slot, the second
                # `set` would overwrite the first.
                await gate.wait()
                seen[label] = current_confluence_client.get().headers[
                    "Authorization"
                ]
            finally:
                current_confluence_client.reset(token)

        task_alice = asyncio.create_task(run_as("alice", alice))
        task_bob = asyncio.create_task(run_as("bob", bob))
        # Yield so both tasks reach `await gate.wait()`.
        await asyncio.sleep(0.01)
        gate.set()
        await asyncio.gather(task_alice, task_bob)

        assert seen["alice"] == alice.headers["Authorization"]
        assert seen["bob"] == bob.headers["Authorization"]
        assert seen["alice"] != seen["bob"]
