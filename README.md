# Confluence MCP Server

A Model Context Protocol (MCP) server for Atlassian Confluence Server 7.4.6, built with FastAPI and the official MCP Python SDK. This server wraps the Confluence REST API and exposes it as MCP tools for use with Claude and other MCP clients.

## Features

- **Complete MCP implementation** over HTTP + SSE transport
- **Full Confluence Server 7.4.6 support** with Storage Format (XHTML)
- **Search & Content Management**: Search, create, read, update, delete pages
- **Comments**: List and add comments to pages
- **Attachments**: List, upload, and download attachments
- **Storage Format conversion** to Markdown (read-only, lossy)
- **Async FastAPI backend** with proper error handling and logging

## Confluence 7.4.6 Gotchas

This server is specifically designed for **Confluence Server 7.4.6** (the last LTS before Server EOL). Key differences from modern Confluence:

- **No Personal Access Tokens (PATs)** - must use Basic Auth (username/password)
- **Storage Format is XHTML**, not ADF (Atlassian Document Format)
- **Version conflicts** require automatic version increment on updates
- **CSRF protection** for file uploads requires `X-Atlassian-Token: no-check` header
- **CAPTCHA protection** may activate after failed login attempts
- **Self-signed certificates** common in corporate environments

## Installation

### Using uv (recommended)

```bash
# Clone or create the project directory
cd confluence

# Install dependencies
uv sync

# Or install in editable mode
uv pip install -e .
```

### Using pip + venv

```bash
# Clone or create the project directory
cd confluence

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -e .
```

## Configuration

This server uses **Basic Auth pass-through**: each MCP client supplies its
own Confluence credentials via `Authorization: Basic` on every request.
The server itself stores no credentials — only the URL of the Confluence
instance it proxies to.

1. **Copy environment template:**
   ```bash
   cp .env.example .env
   ```

2. **Edit `.env` — only the Confluence base URL and server settings:**
   ```env
   # Confluence Server connection
   CONFLUENCE_BASE_URL=https://your-confluence-server.com/confluence
   CONFLUENCE_VERIFY_SSL=true

   # FastAPI server settings
   HOST=localhost
   PORT=8765
   LOG_LEVEL=INFO
   ```

   Note: legacy `CONFLUENCE_USERNAME` / `CONFLUENCE_PASSWORD` variables are
   ignored if present (config has `extra="ignore"` for migration safety).
   Delete them when convenient.

3. **Per-user credentials** are supplied by each MCP client in the
   `Authorization` header — see the *Connecting to Claude* section below.

4. **Required permissions** for each user's Confluence account:
   - Read access to spaces they want to search/read
   - Write access to spaces where they want to create/update content
   - File upload permissions for attachment operations

### CAPTCHA and the dclouds deployment

If a user gets too many 401s in a row, Confluence locks **their own**
Confluence account behind a CAPTCHA challenge. The MCP server will surface
this as `401` on `/sse` with the body
`"Confluence rejected the supplied credentials"`.

Fix: the affected user opens the Confluence UI in a browser, logs in once
(solving the CAPTCHA), and the REST endpoints unstick. No server-side
intervention is needed.

The dclouds installation also has a shared service account
`jira_integration` (used by the sibling `Arkady/` automations for Jira). It
is **not** used by this MCP server — but if you want to test against
dclouds without setting up a personal account, you can put its credentials
in your `.mcp.json` headers temporarily. The password lives in
`Arkady/ArkadyJarvis/.env` and `Arkady/ArkadySuperMan/.env` and is rotated
through the Confluence admin UI:
`https://jira.dclouds.ru/confluence/admin/users/edituser.action?username=jira_integration`.

## Running the Server

### Command Line

```bash
# Using uv
uv run python -m confluence_mcp

# Using pip/venv
python -m confluence_mcp
```

### Environment Variables

You can override settings via environment variables:

```bash
CONFLUENCE_BASE_URL=https://other-server.com LOG_LEVEL=DEBUG python -m confluence_mcp
```

The server starts on `http://127.0.0.1:8765` by default (change via `HOST` / `PORT` in `.env`).

### Health Check

Verify the server process is alive:

```bash
curl http://127.0.0.1:8765/healthz
# → {"status":"healthy"}
```

This is a **liveness probe** — it only confirms the FastAPI app finished
its startup and the HTTP/SSE plumbing is ready. It does **not** call
Confluence: there is no shared service account to authenticate with.
Confluence reachability is verified per user on each `/sse` connection.

## Docker deployment

A `Dockerfile` and `docker-compose.yml` are provided. The image is based on
`python:3.11-slim` (~50 MB) plus ~75 MB of Python deps — final image is
roughly 125–150 MB.

### One-time setup

```bash
# 1. Have .env in this directory (NOT baked into the image — see .dockerignore)
cp .env.example .env
# edit .env: CONFLUENCE_BASE_URL (and CONFLUENCE_VERIFY_SSL if needed).
# Per-user credentials are NOT stored here — see the Configuration section.

# 2. Build and start
docker compose up --build -d

# 3. Tail logs
docker compose logs -f confluence-mcp
```

The container publishes port `8765` on the host. Override with `PORT` in
`.env` (and adjust the `ports:` mapping in `docker-compose.yml` if you change it).

### Where things live

- `.env` is **mounted** by compose (`env_file:`), not copied into the image.
  This keeps the password out of image layers and the Docker registry.
- `HOST` is forced to `0.0.0.0` by `docker-compose.yml` (`environment:` block)
  — your `.env` setting for `HOST` is ignored inside the container, otherwise
  the published port would not reach the process.
- A container `healthcheck` polls `/healthz` every 30 s. `docker ps` will
  show the container as `unhealthy` only when the **process itself** falls
  over — `/healthz` is a liveness probe and does not call Confluence. With
  the Basic Auth pass-through model the server has no shared creds, so
  Confluence reachability/CAPTCHA/wrong-password failures show up at SSE
  connect time (401/502 to the MCP client), not as container health.

### Verify the running container

```bash
curl http://127.0.0.1:8765/healthz
docker inspect --format='{{.State.Health.Status}}' confluence-mcp
```

### Updating after a code change

```bash
docker compose up --build -d   # rebuild + recreate
```

### Connecting from other containers on the same Docker host

If your Claude client / Arkady service runs in its own container on the
same host, reach this server by **container name** rather than `localhost`:

```
http://confluence-mcp:8765/sse
```

(They must share a Docker network; the simplest setup is to put both
services in the same `docker-compose.yml`.)

## Connecting to Claude

This server speaks **MCP over HTTP+SSE**, not stdio. The transport is
`SseServerTransport` from the official MCP SDK, wired into FastAPI:

- `GET  /sse`          — opens an SSE stream and emits the `endpoint` event.
                         Requires `Authorization: Basic <base64(user:pass)>`.
- `POST /messages/`    — JSON-RPC channel (session id as query param). Also
                         requires `Authorization: Basic` (the server enforces
                         the header but does not re-validate against
                         Confluence — the SSE handshake already did).

Each MCP client embeds its **own** Confluence credentials in the
`Authorization` header. Different users connecting to the same server see
different spaces, write under their own accounts, and don't share state.

### Claude Code

`.mcp.json` (or via `claude mcp add`):

```json
{
  "mcpServers": {
    "confluence": {
      "type": "sse",
      "url": "http://127.0.0.1:8765/sse",
      "headers": {
        "Authorization": "Basic <base64(your-username:your-password)>"
      }
    }
  }
}
```

Generate the base64 value (macOS/Linux):

```bash
echo -n "alice:my-confluence-password" | base64
```

Make sure the server is already running before launching Claude Code.

### Claude Desktop

Claude Desktop's stdio integration cannot speak SSE directly. Use the
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote) shim and pass the
auth header via `--header`:

```json
{
  "mcpServers": {
    "confluence": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://127.0.0.1:8765/sse",
        "--header",
        "Authorization: Basic <base64(user:pass)>"
      ]
    }
  }
}
```

### Smoke test via MCP Inspector

```bash
npx @modelcontextprotocol/inspector
# In the UI, choose transport=SSE, URL=http://127.0.0.1:8765/sse
# Add a custom header: Authorization: Basic <base64(user:pass)>
```

## Available Tools

### Search Tools

- **`confluence_search`**: Search content using CQL (Confluence Query Language)
  - Parameters: `cql` (required), `limit`, `start`, `include_excerpt`
  - Example: `cql="space = 'DEMO' and type = 'page' and title ~ 'API'"`

### Page Tools

- **`confluence_get_page`**: Get page by ID or space+title
  - Parameters: `id` OR (`space_key` + `title`), `format` (storage/markdown), `include_version`
- **`confluence_create_page`**: Create new page
  - Parameters: `space_key`, `title`, `content` (Storage Format), `parent_id` (optional)
- **`confluence_update_page`**: Update existing page (auto-handles versioning)
  - Parameters: `id`, `title` (optional), `content` (optional)
- **`confluence_delete_page`**: Delete (trash) a page
  - Parameters: `id`

### Comment Tools

- **`confluence_list_comments`**: List comments on a page
  - Parameters: `page_id`, `limit`, `start`, `format` (storage/markdown)
- **`confluence_add_comment`**: Add comment to page
  - Parameters: `page_id`, `content` (Storage Format)

### Attachment Tools

- **`confluence_list_attachments`**: List attachments on a page
  - Parameters: `page_id`, `limit`, `start`
- **`confluence_download_attachment`**: Download attachment
  - Parameters: `page_id`, `filename`, `output` (base64/file), `file_path` (if output=file)
- **`confluence_upload_attachment`**: Upload attachment
  - Parameters: `page_id`, `file_path`, `comment` (optional)

## Content Formats

### Storage Format (Confluence XHTML)

For creating/updating content, use Confluence Storage Format:

```html
<p>Simple paragraph</p>
<h1>Heading 1</h1>
<h2>Heading 2</h2>
<ul>
  <li>Bullet point</li>
  <li>Another point</li>
</ul>
<strong>Bold text</strong>
<em>Italic text</em>
<a href="https://example.com">Link</a>
```

### Markdown Conversion

The server can convert Storage Format to Markdown for reading, but this is **lossy** and **one-way only**. Confluence macros and advanced formatting will be simplified or lost.

## Testing

### Unit Tests

```bash
# Using uv
uv run pytest

# Using pip/venv
pytest

# Run with coverage
pytest --cov=confluence_mcp

# Run specific test file
pytest tests/test_client.py
```

### Integration Tests

Integration tests require a live Confluence instance:

```bash
# Set up environment for integration testing
export CONFLUENCE_INTEGRATION_TEST=1
export CONFLUENCE_BASE_URL=https://your-test-confluence.com

# Run integration tests
pytest -m integration
```

**Note**: Integration tests will create, modify, and delete test content. Use a dedicated test space.

## Architecture

```
src/confluence_mcp/
├── app.py              # FastAPI app: lifespan, /healthz, _parse_basic_auth,
│                       # /sse (Basic Auth + per-session client), /messages/ mount
├── mcp_server.py       # mcp.Server creation, list_tools registration
├── client.py           # Async Confluence REST client (httpx); per-session
├── config.py           # Pydantic settings from env (server-level only)
├── session.py          # ContextVar + LazyConfluenceClient — per-session plumbing
├── converters.py       # Storage XHTML → Markdown (one-way)
├── errors.py           # Mapped exceptions for REST error responses
└── tools/
    ├── __init__.py     # Handler collector + single call_tool dispatcher (*)
    ├── search.py       # CQL search
    ├── pages.py        # Page CRUD (update auto-increments version)
    ├── comments.py     # List/add comments (container.type=page is required)
    └── attachments.py  # Multipart upload sends X-Atlassian-Token: no-check
```

(\*) The MCP SDK exposes a single global `@server.call_tool()` slot —
registering multiple handlers overwrites it. `tools/__init__.py` collects
handlers from each module via a Server-shaped proxy and installs one
dispatcher that routes by tool name. Handlers receive a `LazyConfluenceClient`
proxy at registration time; it resolves to the calling session's client via
a contextvar set in `/sse`. There is a regression test for the dispatcher in
`tests/test_handlers.py::TestDispatcherCoverage`, and for the per-session
isolation in `tests/test_auth.py::TestContextvarIsolation`.

## Development

### Code Quality

```bash
# Run linting
ruff check src tests

# Format code
ruff format src tests

# Type checking (if you add mypy)
mypy src
```

### Adding New Tools

1. Create tool implementation in appropriate file under `tools/`
2. Add tool to the module's `TOOLS` list
3. Register in `tools/__init__.py`
4. Add behavioural tests in `tests/test_handlers.py` (schema-shape regressions
   go in `tests/test_tools.py`)

### Debugging

Enable debug logging:

```env
LOG_LEVEL=DEBUG
```

This will show all HTTP requests to Confluence (URLs and status codes, but not sensitive data).

## Known Limitations

- **One-way markdown conversion**: Storage Format → Markdown only
- **No macro preservation**: Confluence macros simplified in markdown output
- **Basic retry logic**: Single retry on 5xx errors only
- **No rate limiting**: Client is responsible for respectful API usage
- **No caching**: Every request hits Confluence API directly

## Security Notes

- **Credentials live in the MCP client, not the server**: each user keeps
  their own `Authorization: Basic` value in their `.mcp.json`. The server's
  `.env` contains only the Confluence base URL.
- **HTTPS recommended**: Use HTTPS for Confluence connection in production —
  Basic Auth credentials are otherwise sent in cleartext over the wire.
- **TLS for the MCP server itself**: if MCP clients are not on the same host
  as the server, terminate TLS in front of it (reverse proxy, ingress). The
  same `Authorization` header travels to the server on every request.
- **Network security**: Consider firewall rules for MCP server port.
- **Confluence permissions**: each user should authenticate as themselves;
  the server does not need a privileged service account.

## Troubleshooting

### Connection Issues

1. **Health check fails**: Test manually with `curl http://127.0.0.1:8765/healthz`.
   Expect `{"status":"healthy"}` — this only checks the process is alive,
   not Confluence reachability.
2. **401 Unauthorized on /sse**: Verify the `Authorization: Basic <base64>`
   header in your `.mcp.json` matches a working Confluence account. Test
   the same credentials by hand: `curl -u "user:pass" "$CONFLUENCE_BASE_URL/rest/api/user/current"`.
3. **502 Bad Gateway on /sse**: The MCP server reached Confluence but
   Confluence returned 5xx or the connection failed. Distinct from 401
   so you can tell a creds issue from an outage.
4. **403 Forbidden on /messages/**: The `Authorization` header in your POST
   doesn't match the user that opened the SSE session (the hijacking
   guard). Usually means your MCP client cached an old session — restart it.
5. **SSL errors**: Set `CONFLUENCE_VERIFY_SSL=false` for self-signed certificates.
6. **CAPTCHA activated**: After repeated 401s Confluence locks the *account*
   behind a CAPTCHA. Sign in via the Confluence UI as that user once to
   solve it; the REST endpoints will unstick.
7. **MCP client hangs on `connect`**: macOS / corp networks often expose a local
   HTTP proxy (e.g. Clash, Shadowsocks). `httpx` will route `127.0.0.1` traffic
   *through* the proxy unless told otherwise. Run the MCP client with
   `NO_PROXY=127.0.0.1,localhost` set in its environment.
8. **Old behavior after source change**: Python caches compiled modules under
   `src/**/__pycache__/`. If a restart doesn't pick up edits, remove the cache:
   `find src -name __pycache__ -exec rm -rf {} +`.

### Tool Errors

1. **Permission denied (403)**: Check Confluence user permissions
2. **Version conflict (409)**: Usually auto-handled, but may indicate concurrent edits
3. **Not found (404)**: Verify page ID, space key, or title spelling
4. **Malformed content**: Ensure Storage Format XHTML is valid

### Performance Issues

1. **Slow responses**: Check Confluence server performance
2. **Large attachments**: Consider chunked upload for files >50MB
3. **Many results**: Use pagination with `limit` and `start` parameters

## License

This project is provided as-is for integration with Atlassian Confluence Server 7.4.6.

## Contributing

1. Fork the repository
2. Create feature branch: `git checkout -b feature-name`
3. Add tests for new functionality
4. Ensure all tests pass: `pytest`
5. Submit pull request

---

**Note**: This server is designed specifically for Confluence Server 7.4.6. For newer versions or Confluence Cloud, modifications may be required to handle different API versions and authentication methods.