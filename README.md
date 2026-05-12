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

1. **Copy environment template:**
   ```bash
   cp .env.example .env
   ```

2. **Edit `.env` with your Confluence details:**
   ```env
   # Confluence Server connection
   CONFLUENCE_BASE_URL=https://your-confluence-server.com/confluence
   CONFLUENCE_USERNAME=your-username
   CONFLUENCE_PASSWORD=your-password
   CONFLUENCE_VERIFY_SSL=true

   # FastAPI server settings
   HOST=localhost
   PORT=8765
   LOG_LEVEL=INFO
   ```

3. **Required permissions:** Your Confluence user needs:
   - Read access to spaces you want to search/read
   - Write access to spaces where you want to create/update content
   - File upload permissions for attachment operations

### Service account at dclouds (operational notes)

The `dclouds` deployment uses a shared service account for both Jira and
Confluence. Verified state at 2026-05-12:

| Field | Value |
|---|---|
| Username | `jira_integration` |
| Display name | `Integration User` |
| User key | `ff8080818a9b61e7018bf022c45a001a` |
| Account type | `known` (regular user, not a system/anonymous) |
| Groups | `dc-hr`, `dc-user`, `developers`, `interaction-b24`, `jira-administrators`, `jira-developers`, `jira-users`, `loginaccess`, `users` |

**Visible Confluence spaces (all global, 4 total):**

| Key | Name | Read | Write |
|---|---|---|---|
| `HR` | DC HR — Human resources | ✅ | ✅ |
| `PM` | PM ХХХ | ✅ | ❌ (403 on create) |
| `onboarding` | Онбординг | ✅ | ✅ |
| `PBZ` | Проект "База знаний" | ✅ | ✅ |

`PM` is read-only for this account — write tools will return 403 there. Use
one of the other three spaces for create/update/delete operations. There is
no personal space.

**Shared credentials.** The same `jira_integration` password is used by the
sibling automations in `Arkady/`:

- `Arkady/ArkadyJarvis/.env`  (`JIRA_PASSWORD=…`)
- `Arkady/ArkadySuperMan/.env`

If you rotate the password (see below), update all three `.env` files,
otherwise those services will start failing with 401.

**Verify the account is still working:**

```bash
curl -u "$CONFLUENCE_USERNAME:$CONFLUENCE_PASSWORD" \
  "$CONFLUENCE_BASE_URL/rest/api/user/current"
# Expect 200 + JSON with username=jira_integration
```

If you get HTML or a redirect to a login form, the account is locked by
CAPTCHA after failed logins — open the Confluence UI in a browser, log in
as `jira_integration` once, solve the CAPTCHA, and the REST endpoint will
unstick.

**Rotate the password (Confluence Server 7.4.6).**

1. Sign in as a Confluence admin and go to
   `https://jira.dclouds.ru/confluence/admin/users/edituser.action?username=jira_integration`
   (admin → User Management → search "jira_integration" → Edit).
2. Change password — save.
3. Update the new value in every `.env` listed above and in this project's
   `.env`. Restart the MCP server and any Arkady service that uses it.
4. Re-run the health check from this README to confirm.

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

Verify the server is running and can connect to Confluence:

```bash
curl http://127.0.0.1:8765/healthz
# → {"status":"healthy","confluence":"connected"}
```

This endpoint hits Confluence (`GET /rest/api/space?limit=1`) so a 200 means
auth + reachability are both green.

## Docker deployment

A `Dockerfile` and `docker-compose.yml` are provided. The image is based on
`python:3.11-slim` (~50 MB) plus ~75 MB of Python deps — final image is
roughly 125–150 MB.

### One-time setup

```bash
# 1. Have .env in this directory (NOT baked into the image — see .dockerignore)
cp .env.example .env
# edit .env: CONFLUENCE_BASE_URL, CONFLUENCE_USERNAME, CONFLUENCE_PASSWORD

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
  show the container state as `unhealthy` if Confluence becomes unreachable
  (wrong creds, CAPTCHA, network).

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

- `GET  /sse`          — opens an SSE stream and emits the `endpoint` event
- `POST /messages/`    — JSON-RPC channel (session id is passed as query param)

### Claude Code

`.mcp.json` (or via `claude mcp add`):

```json
{
  "mcpServers": {
    "confluence": {
      "type": "sse",
      "url": "http://127.0.0.1:8765/sse"
    }
  }
}
```

Make sure the server is already running before launching Claude Code.

### Claude Desktop

Claude Desktop's stdio integration cannot speak SSE directly. Use the
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote) shim to bridge:

```json
{
  "mcpServers": {
    "confluence": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8765/sse"]
    }
  }
}
```

### Smoke test via MCP Inspector

```bash
npx @modelcontextprotocol/inspector
# In the UI, choose transport=SSE, URL=http://127.0.0.1:8765/sse
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
export CONFLUENCE_USERNAME=test-user
export CONFLUENCE_PASSWORD=test-password

# Run integration tests
pytest -m integration
```

**Note**: Integration tests will create, modify, and delete test content. Use a dedicated test space.

## Architecture

```
src/confluence_mcp/
├── app.py              # FastAPI app: lifespan, /healthz, /sse, /messages/ mount
├── mcp_server.py       # mcp.Server creation, list_tools registration
├── client.py           # Async Confluence REST client (httpx)
├── config.py           # Pydantic settings from env
├── converters.py       # Storage XHTML → Markdown (one-way)
├── errors.py           # Mapped exceptions for REST error responses
└── tools/
    ├── __init__.py     # Handler collector + single call_tool dispatcher (*)
    ├── search.py       # CQL search
    ├── pages.py        # Page CRUD (update auto-increments version)
    ├── comments.py     # List/add comments (container.type=page is required)
    └── attachments.py  # Multipart upload sends X-Atlassian-Token: no-check
```

(\*) The MCP SDK exposes a single global `@server.call_tool()` slot — registering
multiple handlers overwrites it. `tools/__init__.py` collects handlers from each
module via a Server-shaped proxy and installs one dispatcher that routes by tool
name. There is a regression test for this in `tests/test_tools.py::TestDispatcher`.

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
4. Add tests in `tests/test_tools.py`

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

- **Credentials in environment**: Keep `.env` file secure, never commit it
- **HTTPS recommended**: Use HTTPS for Confluence connection in production
- **Network security**: Consider firewall rules for MCP server port
- **Confluence permissions**: Use principle of least privilege for API user

## Troubleshooting

### Connection Issues

1. **Health check fails**: Test manually with `curl http://127.0.0.1:8765/healthz`
2. **401 Unauthorized**: Verify username/password in `.env`
3. **SSL errors**: Set `CONFLUENCE_VERIFY_SSL=false` for self-signed certificates
4. **CAPTCHA activated**: Use different credentials, or log in via the Confluence UI once to clear it
5. **MCP client hangs on `connect`**: macOS / corp networks often expose a local
   HTTP proxy (e.g. Clash, Shadowsocks). `httpx` will route `127.0.0.1` traffic
   *through* the proxy unless told otherwise. Run the MCP client with
   `NO_PROXY=127.0.0.1,localhost` set in its environment.
6. **Old behavior after source change**: Python caches compiled modules under
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