# Confluence MCP — context for Claude

Python MCP-сервер над **Atlassian Confluence Server 7.4.6** (on-prem, EOL Server-линейка). REST API wrapper, без установки плагина в сам Confluence. Транспорт MCP — **HTTP + SSE** (через `SseServerTransport` от официального MCP SDK, смонтирован в FastAPI), не stdio.

Целевой инстанс: `https://jira.dclouds.ru/confluence`. Auth: **Basic Auth only** (PAT появились только в Confluence 7.9). Креды в `.env`.

## Что важно помнить при правках

### 1. Это Confluence Server 7.4.6, не Cloud и не свежий DC

- Контент — **Storage Format (XHTML)**, не ADF. ADF появился позже и только в Cloud — не использовать.
- **Нет PAT** → только Basic Auth.
- Нет нового search v2 — остаёмся на `/rest/api/content/search` с CQL.
- Комментарии: `POST /rest/api/content` обязательно требует `container.type = "page"`, иначе 500. См. `tools/comments.py:147`.
- Multipart upload: обязательно `X-Atlassian-Token: no-check` (CSRF), добавляется автоматически в `client._request` когда передан `files=`.
- Update страницы — клиент сам делает GET → version+1 → PUT (`client.py:216`). Не пытайся передавать version извне.
- CAPTCHA: после нескольких 401 аккаунт залочат капчей. `/rest/api/user/current` начнёт отдавать HTML вместо JSON. Лечится логином через UI один раз.

### 2. Один глобальный `@server.call_tool()` в MCP SDK

В MCP Python SDK у `Server` есть **один-единственный** слот `call_tool` — каждое новое `@server.call_tool()` затирает предыдущее. Поэтому в `tools/__init__.py` сделан паттерн:

- Каждый модуль (`pages.py`, `search.py`, …) регистрирует свои хендлеры через объект, который **крякает как Server** (`_HandlerCollector`).
- В реальный `Server` регистрируется **один** `_dispatch(name, arguments)`, который роутит по имени тулзы.

Если добавляешь новый модуль с тулзами — повторяй этот же паттерн (`register_X_tools(server, confluence)` + список `X_TOOLS`), и добавь его в `register_all_tools` и `ALL_TOOLS`. Регрессия на это уже есть: `tests/test_tools.py::TestDispatcher`.

### 3. Никаких `oneOf` / `allOf` / `anyOf` на верхнем уровне `inputSchema`

Anthropic API отбрасывает весь запрос с `400 tools.N.custom.input_schema: input_schema does not support oneOf, allOf, or anyOf at the top level`. Это касается **только корня** объекта схемы — внутри `properties.*` они разрешены.

Если нужна логика "либо A, либо (B и C)" — оставь `required` пустым (или с общими полями), а валидацию делай руками в начале хендлера и возвращай `TextContent` с понятной ошибкой. Так уже сделано в `confluence_get_page` (`tools/pages.py:43`). Был инцидент 2026-05-14 — `anyOf` на топ-левеле в `get_page` ронял все запросы клиента, не только к этому тулу.

### 4. Транспорт — SSE, не stdio

- `GET /sse` открывает SSE-стрим, MCP SDK эмитит событие `endpoint`.
- `POST /messages/?session_id=…` — JSON-RPC канал.
- **`SseServerTransport` инстанцируется per-app в `lifespan`** (`app.py:35`), чтобы повторные `create_app()` (в тестах) не делили session-таблицу.
- В тестах FastAPI `TestClient` SSE-протокол **не гоняет** — реальный round-trip нужно проверять `mcp.client.sse.sse_client`. См. ниже про проверку субагентами.

### 5. CRITICAL: верификация субагентов

Если делегируешь работу субагенту (например, `fastapi-expert` или `general-purpose`), **не верь отчёту "N/N тестов прошло"** на словах. На этом проекте уже был случай (2026-05-12): субагент собрал `/sse` и `/messages` как заглушки (`{type: connected}` и `{result: {}}`), 28/28 юнит-тестов зелёные, но настоящий MCP-клиент не подключался.

После любого изменения транспорта или нового тула обязательно прогнать end-to-end:

```python
from mcp.client.sse import sse_client
from mcp import ClientSession
async with sse_client("http://127.0.0.1:8765/sse") as (r, w):
    async with ClientSession(r, w) as s:
        await s.initialize()
        await s.list_tools()
        await s.call_tool("confluence_search", {"cql": "type=page", "limit": 1})
```

или через `npx @modelcontextprotocol/inspector` (transport=SSE).

### 6. Сетевые ловушки на macOS

`httpx` уважает системные прокси. На маках с Clash/Shadowsocks/корпоративным прокси MCP-клиент будет висеть на `connect`, пытаясь дотянуться до `127.0.0.1` через прокси. Решение в окружении клиента: `NO_PROXY=127.0.0.1,localhost`.

### 7. Кеш `__pycache__`

Если после edit поведение не меняется — `find src -name __pycache__ -exec rm -rf {} +` и рестартануть. Часто экономит минуты дебага.

## Карта файлов

```
src/confluence_mcp/
├── __main__.py        # python -m confluence_mcp → uvicorn + create_app()
├── app.py             # FastAPI: lifespan, /healthz, /sse, /messages/ mount
├── mcp_server.py      # mcp.Server, list_tools, register_all_tools
├── client.py          # Async REST client. Retry 5xx только для GET/HEAD/OPTIONS
├── config.py          # pydantic-settings из .env. Поля опциональны для тестов
├── converters.py      # Storage XHTML → Markdown (lossy, one-way; макросы упрощаются)
├── errors.py          # Маппинг HTTP-кодов на типизированные исключения
└── tools/
    ├── __init__.py    # _HandlerCollector + единый _dispatch
    ├── search.py      # confluence_search (CQL)
    ├── pages.py       # get/create/update/delete (update сам инкрементит version)
    ├── comments.py    # list/add (container.type=page обязателен)
    └── attachments.py # list/upload/download (sandbox через _resolve_download_path)
```

## Имена тулзов и их сигнатуры

Все экспортируются под префиксом `confluence_`:

- `confluence_search` — CQL, `limit≤100`, `start`, `include_excerpt`.
- `confluence_get_page` — по `id` ИЛИ (`space_key` + `title`), `format` ∈ {`storage`, `markdown`}.
- `confluence_create_page` — `space_key`, `title`, `content` (storage XHTML), `parent_id?`.
- `confluence_update_page` — `id`, опц. `title` и/или `content` (хотя бы одно). Версия автоматом.
- `confluence_delete_page` — `id` (в trash).
- `confluence_list_comments` / `confluence_add_comment` — `page_id`, для add `content` (storage XHTML).
- `confluence_list_attachments` — `page_id`, пагинация.
- `confluence_download_attachment` — `page_id`, `filename`, `output` ∈ {`base64`, `file`}. При `output=file` каллеровский `file_path` **всегда** сэндбоксится под `CONFLUENCE_DOWNLOAD_DIR`; абсолютные пути и `..` отвергаются.
- `confluence_upload_attachment` — `page_id`, локальный `file_path`, опц. `comment`. Проверка размера на диске **до** чтения в память.

Хард-лимиты в `.env`: `CONFLUENCE_MAX_UPLOAD_BYTES`, `CONFLUENCE_MAX_DOWNLOAD_BYTES` (по умолчанию 25 MiB каждый).

## Сервисный аккаунт dclouds

- Логин `jira_integration`. Пароль лежит **в трёх .env** и должен быть синхронизирован:
  - `mcp/confluence/.env` (`CONFLUENCE_PASSWORD`)
  - `Arkady/ArkadyJarvis/.env` (`JIRA_PASSWORD`)
  - `Arkady/ArkadySuperMan/.env`
- Видит 4 глобальных спейса: `HR`, `onboarding`, `PBZ` (read+write), `PM` (read-only, create → 403).
- Тестовая песочница — **`PBZ`** ("Проект 'База знаний'"). На `PM` write-тесты упадут на 403, это не баг.
- Проверка: `curl -u "$CONFLUENCE_USERNAME:$CONFLUENCE_PASSWORD" "$CONFLUENCE_BASE_URL/rest/api/user/current"` → 200+JSON. HTML/redirect = CAPTCHA, разлочить логином через UI.

## Команды

```bash
# venv установлен в ./venv; альтернатива — uv sync
source venv/bin/activate && python -m confluence_mcp

# unit tests (mock'и через respx, реальный Confluence не нужен)
pytest

# integration тесты (помечены @pytest.mark.integration, пропускаются по умолчанию)
CONFLUENCE_INTEGRATION_TEST=1 pytest -m integration

# линт
ruff check src tests
ruff format src tests

# Docker
docker compose up --build -d
docker compose logs -f confluence-mcp
```

Внутри контейнера `HOST=0.0.0.0` форсится из `docker-compose.yml` (иначе порт не достучится до процесса); значение `HOST` из `.env` игнорируется.

## Подключение клиентов

Claude Code (`.mcp.json`):

```json
{ "mcpServers": { "confluence": { "type": "sse", "url": "http://127.0.0.1:8765/sse" } } }
```

Claude Desktop не умеет SSE нативно — шим через `npx -y mcp-remote http://127.0.0.1:8765/sse`.

## Известные ограничения (живут на TODO)

- Конверсия только Storage → Markdown, обратно нет (намеренно — слишком лоссово).
- Retry — одна попытка, только на 5xx, только для idempotent методов.
- Нет rate limiting на стороне сервера, нет кеша.
- CORS, аутентификация на самом MCP-сервере, sse heartbeat — out of scope MVP.
