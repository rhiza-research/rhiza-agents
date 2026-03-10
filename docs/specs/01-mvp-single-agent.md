# Phase 1: MVP -- Single Agent with MCP Tools

## Goal

A working chat application with Keycloak authentication, a single LangGraph ReAct agent with sheerwater MCP tools, and SQLite persistence. Feature parity with sheerwater-chat but built on the LangGraph stack instead of raw Anthropic API calls with a manual tool loop.

## Prerequisites

None -- this is the first phase. The repo should have `docs/ARCHITECTURE.md` and a `pyproject.toml` with dependencies already configured.

## Files to Create

```
src/rhiza_agents/__init__.py
src/rhiza_agents/main.py
src/rhiza_agents/config.py
src/rhiza_agents/auth.py
src/rhiza_agents/db/__init__.py
src/rhiza_agents/db/sqlite.py
src/rhiza_agents/agents/__init__.py
src/rhiza_agents/agents/tools/__init__.py
src/rhiza_agents/agents/tools/mcp.py
src/rhiza_agents/templates/chat.html
src/rhiza_agents/templates/login.html
src/rhiza_agents/static/chat.js
src/rhiza_agents/static/style.css
```

Also create at the repo root:

```
Dockerfile
docker-compose.yml
keycloak/realm.json
```

## Key APIs & Packages

```python
# Agent creation
from langgraph.prebuilt import create_react_agent
from langchain_anthropic import ChatAnthropic

# MCP tool loading
from langchain_mcp_adapters.client import MultiServerMCPClient

# Conversation state persistence
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

# Web framework
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

# Auth
from authlib.integrations.starlette_client import OAuth

# App database
from databases import Database as DatabaseConnection

# Pydantic for request/response models
from pydantic import BaseModel
```

## Implementation Details

### `config.py` -- Environment-based Configuration

Dataclass with `@classmethod from_env()` that reads:

| Env Var | Required | Default | Purpose |
|---------|----------|---------|---------|
| `KEYCLOAK_URL` | yes | -- | Internal Keycloak URL (backend-to-Keycloak) |
| `KEYCLOAK_PUBLIC_URL` | no | same as KEYCLOAK_URL | Public Keycloak URL (browser-to-Keycloak) |
| `KEYCLOAK_REALM` | yes | -- | Keycloak realm name |
| `KEYCLOAK_CLIENT_ID` | yes | -- | OIDC client ID |
| `KEYCLOAK_CLIENT_SECRET` | yes | -- | OIDC client secret |
| `MCP_SERVER_URL` | no | `http://localhost:8000/sse` | Sheerwater MCP server SSE endpoint |
| `ANTHROPIC_API_KEY` | yes | -- | Claude API key |
| `SECRET_KEY` | yes | -- | Session cookie signing key |
| `DATABASE_URL` | no | `sqlite:///./rhiza_agents.db` | App database URL |
| `CHECKPOINT_DB_PATH` | no | `./checkpoints.db` | SQLite path for LangGraph checkpointer |
| `BASE_URL` | no | `http://localhost:8080` | App's own public URL (for OAuth callback) |

### `auth.py` -- Keycloak OIDC

Adapt directly from sheerwater-chat's `auth.py`. Same dual-URL strategy for Docker (internal URL for backend token exchange, public URL for browser redirects).

Functions to provide:
- `create_oauth(config) -> OAuth` -- registers the "keycloak" OAuth provider
- `get_user_from_session(request) -> dict | None`
- `get_user_id(request) -> str | None` -- returns the Keycloak `sub` claim
- `get_user_name(request) -> str | None`

The OAuth registration must use:
- `authorize_url` = public Keycloak URL (browser navigates here)
- `access_token_url` = internal Keycloak URL (backend calls this)
- `userinfo_endpoint` = internal Keycloak URL
- `jwks_uri` = internal Keycloak URL
- `client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"}`

### `db/sqlite.py` -- App Database

This database stores conversation metadata only. Messages are NOT stored here -- they live in the LangGraph checkpointer.

**Schema:**

```sql
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations(user_id);
```

**Class: `Database`**

Constructor takes `database_url: str`, creates a `databases.Database` instance.

Methods:
- `connect()` / `disconnect()` -- lifecycle
- `_init_db()` -- runs CREATE TABLE/INDEX statements
- `create_conversation(conversation_id, user_id, title=None) -> dict`
- `get_conversation(conversation_id, user_id) -> dict | None` -- ownership check
- `list_conversations(user_id, limit=50) -> list[dict]` -- ordered by `updated_at DESC`
- `update_conversation_title(conversation_id, user_id, title)`
- `touch_conversation(conversation_id)` -- updates `updated_at` to now
- `delete_conversation(conversation_id, user_id)` -- deletes row (checkpointer data is left orphaned, acceptable for now)

### `agents/tools/mcp.py` -- MCP Tool Loading

Use `langchain-mcp-adapters` to connect to the sheerwater MCP server and get LangChain-compatible tools.

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

def create_mcp_client(mcp_server_url: str) -> MultiServerMCPClient:
    """Create an MCP client configured for the sheerwater server."""
    return MultiServerMCPClient(
        {
            "sheerwater": {
                "url": mcp_server_url,
                "transport": "sse",
            }
        }
    )
```

**Important**: As of `langchain-mcp-adapters` 0.1.0+, `MultiServerMCPClient` is NOT an async context manager. Call `await client.get_tools()` directly to get the list of LangChain tool objects. Store the tools as a module-level global.

The MCP server may not be ready when the app starts (especially in Docker Compose). Use a retry loop with backoff when calling `client.get_tools()` during startup.

### `main.py` -- FastAPI Application

**Lifespan handler** (async context manager):
1. Load config from env
2. Connect to app database
3. Create MCP client and load tools with retry loop (MCP server may not be ready in Docker Compose)
4. Create `AsyncSqliteSaver` checkpointer (from `CHECKPOINT_DB_PATH`) as async context manager
5. Create `ChatAnthropic` model instance
6. Build the LangGraph agent with `create_react_agent(model, tools, checkpointer=checkpointer)`
7. Create OAuth client
8. Yield (app runs)
9. Cleanup: disconnect database (checkpointer cleans up via its context manager)

Store these as module-level globals: `config`, `db`, `agent`, `oauth`, `mcp_tools`.

**Session middleware**: Add `SessionMiddleware` at module level (not inside `run()`) so it works with uvicorn reload:
```python
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY", "dev-secret-key"))
```

**Uvicorn reload**: Use import string format with `reload_dirs` for hot reload in Docker:
```python
uvicorn.run("rhiza_agents.main:app", host="0.0.0.0", port=8080, reload=True, reload_dirs=["/app/src"])
```

**Auth dependency**: `require_auth(request)` -- returns user dict or raises 401.

**Routes:**

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | optional | If logged in, render chat.html; otherwise, render login.html |
| GET | `/c/{conversation_id}` | required | View a specific conversation |
| GET | `/login` | none | Redirect to Keycloak |
| GET | `/callback` | none | Handle Keycloak OIDC callback, store user in session |
| GET | `/logout` | none | Clear session, redirect to `/` |
| POST | `/api/chat` | required | Send a message, get agent response |
| GET | `/api/conversations` | required | List user's conversations |
| DELETE | `/api/conversations/{id}` | required | Delete a conversation |
| GET | `/api/conversations/{id}/messages` | required | Full message history (debug/evaluation) |
| GET | `/api/tools` | required | List available MCP tools |

**POST /api/chat** request/response:

Request body:
```json
{
    "message": "string",
    "conversation_id": "string | null"
}
```

Handler logic:
1. If no `conversation_id`, generate a UUID, create conversation in app DB
2. Invoke the agent: `await agent.ainvoke({"messages": [HumanMessage(content=message)]}, config={"configurable": {"thread_id": conversation_id}})`
3. Find the new turn's messages: scan backward through `result["messages"]` for the last `HumanMessage`, slice from there
4. Process with `_process_messages()` to separate main chat messages from activity data
5. Extract the final AI text from the processed messages
6. Update conversation title if it's the first message (first 50 chars of user message)
7. Touch the conversation's `updated_at`
8. Return response

Response body:
```json
{
    "conversation_id": "string",
    "response": "string",
    "activity": [
        {"type": "thinking", "content": "Let me look up..."},
        {"type": "tool_call", "name": "tool_run_metric", "args": {...}},
        {"type": "tool_result", "name": "tool_run_metric", "content": {...}}
    ]
}
```

The `activity` field contains the agent's intermediate work for this turn — thinking text from intermediate AI messages, tool calls with their parameters, and tool results. This data is displayed in the activity panel, not in the main chat.

**GET `/c/{conversation_id}`** handler:
1. Verify conversation belongs to user
2. Load conversation history: `await agent.aget_state({"configurable": {"thread_id": conversation_id}})`
3. Process messages with `_process_messages()` to separate main chat from activity
4. Render chat.html with messages and activity data (activity embedded as JSON in a `<script>` tag)

**GET `/api/conversations/{conversation_id}/messages`** — debug/evaluation endpoint:

Returns the full ordered message history for a conversation, processed through `_process_messages()`. Useful for debugging and evaluating agent performance.

Response body:
```json
{
    "conversation_id": "string",
    "messages": [
        {"type": "human", "content": "..."},
        {"type": "thinking", "content": "Let me look up...", "agent_name": "Data Analyst"},
        {"type": "tool_call", "name": "tool_run_metric", "args": {...}},
        {"type": "tool_result", "name": "tool_run_metric", "content": {...}},
        {"type": "ai", "content": "final response text", "agent_name": "Data Analyst"}
    ]
}
```

Uses the same `_process_messages()` as the chat UI and API. Messages are returned as a single flat ordered list with type fields. AI responses and thinking items include `agent_name` when known. Handoff messages are filtered out. Tool result content is parsed as JSON when possible.

**GET `/api/tools`** handler:
1. Return list of tool names and descriptions from the loaded MCP tools

### `templates/login.html` -- Login Page

Simple centered page with app title "Rhiza Agents" and a "Sign in with Keycloak" button linking to `/login`. Uses the same CSS as the chat page.

### `templates/chat.html` -- Chat UI

Three-column layout: sidebar | chat area | activity panel.

```
┌──────────┬─────────────────────┬───────────────┐
│ sidebar  │  chat-area          │ activity-panel│
│ 260px    │  flex: 1            │ 380px         │
│          │                     │ (toggleable)  │
└──────────┴─────────────────────┴───────────────┘
```

Key elements:
- Title: "Rhiza Agents"
- Welcome message: "Ask questions about weather forecast models and data analysis."
- No settings modal (no per-user settings in phase 1)
- Chat header with "Activity" toggle button (top-right of chat area)
- Activity panel on the right with header (title + close button) and scrollable content
- Activity data embedded as `<script id="activity-data" type="application/json">` for server-side pages

Message rendering for server-rendered messages (on page load via Jinja):
- User messages: render the `content` field
- AI messages: render the `content` field with markdown (CSS class `needs-render`, rendered client-side by `marked`)
- Tool calls and intermediate thinking are NOT shown in the main chat — they appear only in the activity panel

The template receives:
- `request`, `user_name`, `conversations` (list), `current_conversation` (dict or None), `messages` (list of human + final AI messages), `activity_json` (JSON string of per-turn activity data)

### `static/chat.js` -- Chat JavaScript

Key pieces:
- `marked` + `highlight.js` for markdown rendering (ESM CDN imports)
- Form submit handler: POST to `/api/chat`, handle response
- Auto-resize textarea
- Enter to send, Shift+Enter for newline
- Loading state with "Thinking..." animation
- On new conversation: update URL to `/c/{id}` via `history.pushState`
- Render server-side messages (those with `needs-render` class) through marked on page load
- Activity panel toggle with localStorage persistence
- `renderActivityItem(item)` — renders a single activity item (thinking, tool call, or tool result) in the activity panel
- On page load: parse embedded `<script id="activity-data">` JSON and render each item via `activityData.forEach(item => renderActivityItem(item))`
- On live chat: use `data.activity` from API response to render new activity items

### `static/style.css` -- Dark Theme

Dark theme with activity panel styles:
- Login page, sidebar, messages, input area, markdown styles, loading animation
- Chat header with activity toggle button
- Activity panel (380px, dark background `#16162a`, toggleable via `.hidden` class with smooth CSS transition)
- Activity items styled by type: thinking (italic, muted), tool-call (blue left border `#4a90d9`), tool-result (green left border `#2ea043`)
- Collapsible details for tool parameters and results

### `Dockerfile`

Follow sheerwater-chat's pattern:
```dockerfile
FROM python:3.12-slim
ARG GIT_SHA=unknown
ARG BUILD_TIMESTAMP=unknown
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
ENV GIT_SHA=${GIT_SHA}
ENV BUILD_TIMESTAMP=${BUILD_TIMESTAMP}
CMD ["uv", "run", "rhiza-agents"]
```

The `rhiza-agents` script entry point should be defined in `pyproject.toml` as:
```toml
[project.scripts]
rhiza-agents = "rhiza_agents.main:run"
```

### `docker-compose.yml`

Three services:

1. **keycloak**: `quay.io/keycloak/keycloak:25.0`, start-dev with realm import, port 8180:8080
2. **sheerwater-mcp**: `ghcr.io/rhiza-research/sheerwater/mcp:latest`, port 8000:8000, mount GCP credentials
3. **rhiza-agents**: build from `.`, port 8080:8080, mount `./src` for hot reload, env vars for Keycloak/MCP/Anthropic

Environment variables for rhiza-agents service:
```yaml
environment:
  KEYCLOAK_URL: http://keycloak:8080
  KEYCLOAK_PUBLIC_URL: http://localhost:8180
  KEYCLOAK_REALM: sheerwater
  KEYCLOAK_CLIENT_ID: rhiza-agents
  KEYCLOAK_CLIENT_SECRET: dev-secret
  ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
  SECRET_KEY: dev-secret-key
  MCP_SERVER_URL: http://sheerwater-mcp:8000/sse
  BASE_URL: http://localhost:8080
  DATABASE_URL: sqlite:////data/rhiza_agents.db
  CHECKPOINT_DB_PATH: /data/checkpoints.db
```

### `keycloak/realm.json`

Copy from sheerwater-chat's keycloak/realm.json. Update the client ID from "sheerwater-chat" to "rhiza-agents" and the client secret to "dev-secret". The realm name stays "sheerwater" (it's the shared Keycloak realm).

### Message Extraction and Processing

When loading a conversation for display, get the checkpoint state and extract messages:

```python
state = await agent.aget_state({"configurable": {"thread_id": conversation_id}})
raw_messages = state.values.get("messages", [])
```

Each message in the list is a LangChain message object. **Important**: `AIMessage.content` can be either a string or a list of content blocks (e.g., `[{"type": "text", "text": "..."}, {"type": "tool_use", ...}]`). Use a helper function to extract text content from either format.

The `_process_messages(raw_messages)` helper function returns a single flat ordered list. Each item has a `type` field (`"human"`, `"ai"`, `"thinking"`, `"tool_call"`, `"tool_result"`). Callers filter by type to separate main chat messages from activity data:

```python
all_messages = _process_messages(raw_messages)
chat_messages = [m for m in all_messages if m["type"] in ("human", "ai")]
activity = [m for m in all_messages if m["type"] in ("thinking", "tool_call", "tool_result")]
```

Classification logic uses `_classify_text(text, has_tool_calls)` with three-tier priority:
1. Explicit `[THINKING]` / `[RESPONSE]` tags in agent output (highest priority)
2. If the `AIMessage` has `tool_calls`, its text is classified as `"thinking"`
3. Otherwise, the text is classified as `"ai"` (response)

This keeps the main chat clean (just human questions and AI answers) while the activity panel shows the agent's behind-the-scenes work.

## Reference Files

Read these files to understand the patterns being adapted:

| File | What to learn |
|------|---------------|
| `/Users/tristan/Devel/rhiza/sheerwater-chat/src/sheerwater_chat/main.py` | FastAPI app structure, lifespan, routes, auth flow |
| `/Users/tristan/Devel/rhiza/sheerwater-chat/src/sheerwater_chat/auth.py` | Keycloak OIDC with dual URL strategy |
| `/Users/tristan/Devel/rhiza/sheerwater-chat/src/sheerwater_chat/config.py` | Env-based config dataclass |
| `/Users/tristan/Devel/rhiza/sheerwater-chat/src/sheerwater_chat/database.py` | Async database with `databases` package |
| `/Users/tristan/Devel/rhiza/sheerwater-chat/src/sheerwater_chat/chat.py` | Chat service (for understanding what we're replacing with LangGraph) |
| `/Users/tristan/Devel/rhiza/sheerwater-chat/src/sheerwater_chat/mcp_client.py` | MCP connection (we replace this with langchain-mcp-adapters) |
| `/Users/tristan/Devel/rhiza/sheerwater-chat/src/sheerwater_chat/templates/chat.html` | Chat UI template |
| `/Users/tristan/Devel/rhiza/sheerwater-chat/src/sheerwater_chat/templates/login.html` | Login page template |
| `/Users/tristan/Devel/rhiza/sheerwater-chat/src/sheerwater_chat/static/chat.js` | Chat JavaScript |
| `/Users/tristan/Devel/rhiza/sheerwater-chat/src/sheerwater_chat/static/style.css` | Dark theme CSS |
| `/Users/tristan/Devel/rhiza/sheerwater-chat/Dockerfile` | Docker build pattern |
| `/Users/tristan/Devel/rhiza/sheerwater-chat/docker-compose.yml` | Docker Compose services |
| `/Users/tristan/Devel/rhiza/rhiza-agents/docs/ARCHITECTURE.md` | Overall architecture reference |

## Acceptance Criteria

1. `docker compose up` starts all three services (Keycloak, MCP, app) without errors
2. Navigate to `http://localhost:8080`, see login page
3. Click "Sign in with Keycloak", authenticate (create a user in Keycloak dev mode first)
4. After login, see the chat UI with empty conversation list
5. Type "list available forecast models" and send
6. Agent calls the MCP tool, final response appears in the main chat. Tool calls and intermediate thinking appear in the activity panel on the right.
7. Conversation appears in the sidebar with the first message as the title
8. Refresh the page -- conversation and messages persist
9. Click the conversation in the sidebar -- messages reload from checkpointer
10. Create a second conversation, both appear in sidebar
11. Delete a conversation via the UI (or API call)
12. `/api/tools` returns the list of available MCP tools

## What NOT to Do

- **No supervisor agent** -- use a single `create_react_agent` directly. The supervisor pattern comes in Phase 2.
- **No config editor** -- no UI for editing system prompts or agent settings. That's Phase 3.
- **No sandbox execution** -- no Daytona integration. That's Phase 4.
- **No vector stores** -- no ChromaDB or document upload. That's Phase 5.
- **No streaming** -- the `/api/chat` endpoint returns the full response at once. SSE streaming comes in Phase 6.
- **No GitHub Actions or Helm chart** -- local development only. Production deployment is Phase 7.
- **No message storage in app DB** -- messages live only in the LangGraph checkpointer. The app DB stores only conversation metadata (id, user_id, title, timestamps).
