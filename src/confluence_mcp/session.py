"""Per-session Confluence client plumbing.

The SSE handler authenticates an incoming MCP client via `Authorization: Basic`,
builds a `ConfluenceClient` with those credentials, and publishes it on a
contextvar. The MCP tool dispatcher and every tool handler downstream reach
back to that contextvar through `LazyConfluenceClient` instead of capturing a
single client at registration time.

Why a contextvar:
- `mcp_server.run(...)` is a long-running coroutine that owns one SSE session.
  All tool dispatches for that session execute in the same asyncio task tree,
  so contextvars set before `run` are visible inside every handler call.
- POST /messages/ deposits incoming JSON-RPC into the session's `read_stream`
  but does NOT run handlers itself — handlers run inside `mcp_server.run`'s
  task, which already has the right contextvar.
- No global mutable mapping by session_id is needed; lifetimes follow the
  asyncio task tree, not application state.
"""

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import ConfluenceClient


current_confluence_client: ContextVar["ConfluenceClient"] = ContextVar(
    "current_confluence_client"
)


class LazyConfluenceClient:
    """Attribute proxy that resolves to whatever client lives in the contextvar.

    Tool handlers were written to take a `confluence` object and call methods on
    it directly (`await confluence.search_content(...)`). Rather than rewrite
    nine handlers to pull from the contextvar themselves, we hand them this
    proxy at registration time and let attribute access do the lookup.

    Raises `LookupError` if there's no active session — that should only happen
    if a handler is invoked outside the SSE pipeline (e.g. a misconfigured
    test).
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        try:
            client = current_confluence_client.get()
        except LookupError as exc:
            raise LookupError(
                "No active Confluence session — tool handlers must run inside "
                "an authenticated /sse connection (or have "
                "`current_confluence_client` set explicitly in tests)."
            ) from exc
        return getattr(client, name)
