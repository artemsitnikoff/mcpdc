"""FastAPI application factory for Confluence MCP server.

Multi-tenant auth model: every MCP client supplies its own Confluence
credentials via `Authorization: Basic` on every request. The server has no
shared service account — it only knows the Confluence base URL.

The /sse handler:
  1) parses Basic Auth from the incoming request,
  2) builds a per-session `ConfluenceClient` with those credentials,
  3) probes `GET /rest/api/user/current` once to validate (no caching of bad
     creds, but no re-probing on every JSON-RPC call either),
  4) publishes the client on the `current_confluence_client` contextvar for
     the lifetime of `mcp_server.run(...)`,
  5) tears the context down when the SSE stream closes.

POST /messages/ requires the same `Authorization: Basic` header but does NOT
re-validate against Confluence — the SSE handshake already proved the caller
knows valid credentials, and the session_id is the binding between the two
endpoints.
"""

import asyncio
import base64
import binascii
import logging
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncGenerator, Dict, Tuple
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, HTTPException, Request
from mcp.server.sse import SseServerTransport
from starlette.responses import JSONResponse

from .client import ConfluenceClient
from .config import get_settings
from .errors import ConfluenceAuthError, ConfluenceError, ConfluencePermissionError
from .mcp_server import create_mcp_server
from .session import current_confluence_client

logger = logging.getLogger(__name__)

_WWW_AUTH_HEADERS = {"WWW-Authenticate": 'Basic realm="confluence-mcp"'}

# session_id is the URL-shape string the MCP SDK puts into the endpoint
# event (`?session_id=<value>`). Today that's `UUID.hex` (32 chars, no
# dashes); the alias makes accidental substitution with `str(uuid)`
# (36 chars, with dashes) more visible in types — the `session_owners`
# keys and the parsed querystring on /messages/ MUST use the same form.
SessionId = str


def _parse_basic_auth(header_value: str | None) -> Tuple[str, str]:
    """Return (username, password) or raise HTTPException(401).

    Accepts only `Basic <base64>` — anything else (missing header, Bearer
    tokens, malformed base64, missing colon) is treated as unauthenticated.

    RFC 7235 technically allows case-insensitive scheme names and arbitrary
    WSP between scheme and credentials. We enforce the canonical `Basic `
    (capital B, single space) on purpose: the `WWW-Authenticate: Basic`
    header we return in our 401 responses makes spec-compliant clients send
    exactly that form, so any deviation is either a buggy client or a
    probe — easier to debug if we reject it loudly.
    """
    if not header_value or not header_value.startswith("Basic "):
        raise HTTPException(
            status_code=401,
            detail="Missing Basic Auth credentials",
            headers=_WWW_AUTH_HEADERS,
        )
    encoded = header_value[len("Basic ") :].strip()
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=401,
            detail="Malformed Basic Auth header",
            headers=_WWW_AUTH_HEADERS,
        ) from exc
    if ":" not in decoded:
        raise HTTPException(
            status_code=401,
            detail="Malformed Basic Auth header",
            headers=_WWW_AUTH_HEADERS,
        )
    username, password = decoded.split(":", 1)
    if not username or not password:
        raise HTTPException(
            status_code=401,
            detail="Username and password are required",
            headers=_WWW_AUTH_HEADERS,
        )
    return username, password


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Starting Confluence MCP server...")

    settings = get_settings()

    # One httpx pool serves every per-session ConfluenceClient — the clients
    # themselves are cheap (just headers + ref to settings) and we don't want
    # to spin up a new connection pool per MCP session.
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        verify=settings.confluence_verify_ssl,
    )

    mcp_server = create_mcp_server()
    # Per-app SSE transport: keeping it per-instance means multiple
    # `create_app()` calls (notably in tests) don't share an in-memory session
    # table.
    sse_transport = SseServerTransport("/messages/")

    # session_id -> Confluence username that owns the SSE stream. Filled
    # in by /sse after the auth probe succeeds, popped at cleanup.
    # /messages/ uses it to reject POSTs whose Basic-Auth user doesn't match
    # the session's owner — the session hijacking guard.
    session_owners: Dict[SessionId, str] = {}

    # Serialises the diff-the-keys trick we use to recover session_id from
    # SseServerTransport. Without this lock, two concurrent /sse connections
    # can both observe the same `before` snapshot and then see two new keys
    # in `after`, forcing both into the len(new_keys)==1 failure path.
    #
    # Known limitation: the lock currently spans `enter_async_context(
    # connect_sse(...))`, which means anything the SDK does inside
    # connect_sse (today: in-memory `_security.validate_request`) runs
    # under the lock. If the SDK ever adds network I/O there (DNS lookup
    # for a `host` check, for example), /sse connect throughput becomes
    # serial. Sub-millisecond today, but not free in the worst case. The
    # alternative — vendoring or subclassing `SseServerTransport` so we
    # learn the new session_id without diffing a private dict — was
    # judged not worth the maintenance cost while the SDK is in flux.
    session_register_lock = asyncio.Lock()

    app.state.settings = settings
    app.state.http_client = http_client
    app.state.mcp_server = mcp_server
    app.state.sse_transport = sse_transport
    app.state.session_owners = session_owners
    app.state.session_register_lock = session_register_lock

    logger.info("Confluence MCP server started successfully")

    yield

    logger.info("Shutting down Confluence MCP server...")
    await http_client.aclose()
    logger.info("Confluence MCP server stopped")


async def _build_session_client(
    request: Request,
) -> ConfluenceClient:
    """Authenticate the SSE caller and return a Confluence client.

    Raises HTTPException(401) for malformed/missing credentials and for
    credentials that Confluence rejects.
    """
    username, password = _parse_basic_auth(request.headers.get("Authorization"))

    settings = getattr(request.app.state, "settings", None)
    http_client = getattr(request.app.state, "http_client", None)
    if settings is None or http_client is None:
        raise HTTPException(status_code=503, detail="Service not initialized")

    client = ConfluenceClient(settings, username, password)
    client.set_http_client(http_client)

    # One validation probe per session. `validate_credentials` (unlike the
    # liberal `health_check`) propagates auth vs transport failures as
    # distinct exception types, so we can return 401 to the user when their
    # creds are wrong and 502 when Confluence is just unreachable. Without
    # this distinction users go chase password issues during outages.
    try:
        await client.validate_credentials()
    except ConfluenceAuthError as exc:
        logger.info("Rejected SSE connect for %r: %s", username, exc.message)
        raise HTTPException(
            status_code=401,
            detail="Confluence rejected the supplied credentials",
            headers=_WWW_AUTH_HEADERS,
        ) from exc
    except ConfluencePermissionError as exc:
        # `ConfluencePermissionError` inherits from `ConfluenceError`, so this
        # ordering matters: without an explicit branch, a 403 from Confluence
        # would fall through into the generic `ConfluenceError` handler and
        # surface as 502 "unreachable", sending the user to chase an outage
        # when the real fix is "ask an admin for read access".
        logger.info("Account %r lacks read access: %s", username, exc.message)
        raise HTTPException(
            status_code=403,
            detail="Confluence rejected this account (no read access)",
        ) from exc
    except ConfluenceError as exc:
        logger.warning("Confluence unreachable during auth probe: %s", exc.message)
        raise HTTPException(
            status_code=502,
            detail="Confluence unreachable",
        ) from exc

    return client


def create_app() -> FastAPI:
    app = FastAPI(
        title="Confluence MCP Server",
        description="Model Context Protocol server for Atlassian Confluence Server 7.4.6",
        version="0.2.0",
        lifespan=lifespan,
    )

    @app.get("/healthz")
    async def health_check():
        """Liveness probe: process is up and lifespan finished.

        Does NOT call Confluence — this server has no shared credentials, so
        Confluence reachability is now a per-user concern surfaced at SSE
        connect time.
        """
        if not getattr(app.state, "http_client", None):
            raise HTTPException(status_code=503, detail="Service not initialized")
        if not getattr(app.state, "sse_transport", None):
            raise HTTPException(status_code=503, detail="Service not initialized")
        return {"status": "healthy"}

    @app.get("/")
    async def root():
        try:
            confluence_url = get_settings().confluence_base_url
        except Exception:
            confluence_url = "Not configured"
        return {
            "name": "Confluence MCP Server",
            "version": "0.2.0",
            "description": "Model Context Protocol server for Atlassian Confluence Server 7.4.6",
            "auth": "Basic Auth pass-through (per-user credentials, no shared service account)",
            "endpoints": {
                "health": "/healthz",
                "mcp_sse": "/sse",
                "mcp_messages": "/messages/",
            },
            "confluence_url": confluence_url,
        }

    @app.get("/sse")
    async def sse_endpoint(request: Request):
        sse_transport: SseServerTransport | None = getattr(
            app.state, "sse_transport", None
        )
        mcp_server = getattr(app.state, "mcp_server", None)
        if not sse_transport or not mcp_server:
            raise HTTPException(status_code=503, detail="MCP server not initialized")

        confluence = await _build_session_client(request)
        log_id = uuid.uuid4().hex[:8]
        logger.info(
            "SSE session %s opened for user %r", log_id, confluence.username
        )

        # SseServerTransport generates a fresh session_id inside `connect_sse`
        # and stows it in `_read_stream_writers` before yielding the streams.
        # Diffing the dict's keys before/after entry recovers the id without
        # monkey-patching uuid4 or subclassing the transport. The attribute
        # is private but stable across the MCP SDK versions we pin in
        # `pyproject.toml`. The register-lock makes the diff-trick safe under
        # concurrent /sse connections (see lifespan); the actual MCP loop
        # runs outside the lock so sessions don't serialise.
        async with AsyncExitStack() as stack:
            async with app.state.session_register_lock:
                before = set(sse_transport._read_stream_writers.keys())
                streams = await stack.enter_async_context(
                    sse_transport.connect_sse(
                        request.scope, request.receive, request._send
                    )
                )
                new_keys = set(sse_transport._read_stream_writers.keys()) - before
                # The transport advertises session_id in the URL using `.hex`
                # (no dashes). Use the same form as the key in session_owners
                # so /messages/ can match incoming `?session_id=…`.
                new_key = next(iter(new_keys)) if len(new_keys) == 1 else None
                session_id = new_key.hex if new_key is not None else None
                if session_id is None:
                    logger.error(
                        "SSE session %s: could not determine session_id "
                        "(transport diff yielded %d new keys); refusing connection",
                        log_id, len(new_keys),
                    )
                    raise HTTPException(
                        status_code=500,
                        detail="Could not register session ownership",
                    )

                app.state.session_owners[session_id] = confluence.username
                # Cleanup order is AsyncExitStack-LIFO (not "lock release"):
                # callbacks registered LAST run FIRST. So the order at exit
                # will be:
                #   1. session_owners.pop(session_id)
                #         → any in-flight /messages/ POST instantly hits
                #           the unknown-owner 404 branch — fail closed
                #           at our layer.
                #   2. _read_stream_writers.pop(new_key)
                #         → the SDK's handle_post_message stops finding
                #           the writer, returns its own 404.
                #   3. connect_sse.__aexit__()
                #         → SDK task group teardown, stream aclose.
                # The lock is already released by the time these run; the
                # cleanup ops themselves are dict-pop, Python's GIL keeps
                # them atomic.
                stack.callback(
                    sse_transport._read_stream_writers.pop, new_key, None
                )
                stack.callback(
                    app.state.session_owners.pop, session_id, None
                )

            read_stream, write_stream = streams
            ctx_token = current_confluence_client.set(confluence)
            try:
                await mcp_server.run(
                    read_stream,
                    write_stream,
                    mcp_server.create_initialization_options(),
                )
            finally:
                current_confluence_client.reset(ctx_token)
                logger.info(
                    "SSE session %s closed for user %r",
                    log_id, confluence.username,
                )

    async def _messages_asgi(scope, receive, send):
        """ASGI sub-app for /messages/.

        Two checks before handing off to the SSE transport:

        1. `Authorization: Basic` must be a well-formed header. Without it
           the transport would log a confusing "session not found" for
           unauthenticated noise.

        2. The username in the header must match the owner registered in
           `session_owners[session_id]` at /sse time. This prevents session
           hijacking: even if a session_id leaks (proxy access log, screen
           share, verbose client logs), an attacker with their own valid
           Confluence creds can't piggy-back on someone else's session
           because the binding is checked here. Mismatch → 403, not 401:
           the credentials *are* valid, just for a different session.
        """
        sse_transport = getattr(app.state, "sse_transport", None)
        session_owners: Dict[str, str] | None = getattr(
            app.state, "session_owners", None
        )
        if sse_transport is None or session_owners is None:
            response = JSONResponse(
                {"error": "MCP server not initialized"}, status_code=503
            )
            await response(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        try:
            username, _ = _parse_basic_auth(headers.get("authorization"))
        except HTTPException as exc:
            response = JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers or {},
            )
            await response(scope, receive, send)
            return

        query = parse_qs(scope.get("query_string", b"").decode())
        session_id = query.get("session_id", [None])[0]
        owner = session_owners.get(session_id) if session_id else None
        if owner is None:
            # No session_id, or the session was never registered (closed,
            # made up, or stale). Reject here rather than handing the
            # request to the transport: the transport's own "session not
            # found" path can race with stream cleanup and raise
            # ClosedResourceError, which surfaces as an opaque 500 to the
            # caller. Fail closed with a clear 404 instead. Log so that
            # security review can tell a stale-session retry from a
            # hijack probe with a guessed session_id.
            logger.info(
                "Refused /messages/ POST: session_id %r unknown (auth user %r)",
                session_id, username,
            )
            response = JSONResponse(
                {"detail": "Could not find session for this id"},
                status_code=404,
            )
            await response(scope, receive, send)
            return
        if owner != username:
            logger.warning(
                "Refused /messages/ POST: session %s belongs to %r, "
                "request authenticated as %r",
                session_id, owner, username,
            )
            response = JSONResponse(
                {"detail": "Session does not belong to this user"},
                status_code=403,
            )
            await response(scope, receive, send)
            return

        await sse_transport.handle_post_message(scope, receive, send)

    app.mount("/messages/", _messages_asgi)

    return app
