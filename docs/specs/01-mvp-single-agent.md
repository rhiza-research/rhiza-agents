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

async def get_mcp_tools(mcp_server_url: str) -> list:
    """Load tools from the MCP server.

    Returns LangChain-compatible tool objects that can be passed
    directly to create_react_agent.
    """
    client = MultiServerMCPClient(
        {
            "sheerwater": {
                "url": mcp_server_url,
                "transport": "sse",
            }
        }
    )
    # MultiServerMCPClient is an async context manager
    # The caller must manage its lifecycle
    return client
```

The `MultiServerMCPClient` must be used as an async context manager. During lifespan startup, enter the context manager and call `client.get_tools()` to get the list of LangChain tool objects. Store the client and tools as module-level globals (same pattern as sheerwater-chat's global `mcp_client`).

### `main.py` -- FastAPI Application

**Lifespan handler** (async context manager):
1. Load config from env
2. Connect to app database
3. Enter `MultiServerMCPClient` context manager, load MCP tools
4. Create `AsyncSqliteSaver` checkpointer (from `CHECKPOINT_DB_PATH`)
5. Create `ChatAnthropic` model instance
6. Build the LangGraph agent with `create_react_agent(model, tools, checkpointer=checkpointer)`
7. Create OAuth client
8. Yield (app runs)
9. Cleanup: disconnect database (checkpointer and MCP client clean up via their context managers)

Store these as module-level globals: `config`, `db`, `agent`, `oauth`, `checkpointer`.

**Session middleware**: Add `SessionMiddleware` with `config.secret_key`.

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
3. Extract the last AI message from the response state
4. Extract tool call information from the message history
5. Update conversation title if it's the first message (first 50 chars of user message)
6. Touch the conversation's `updated_at`
7. Return response

Response body:
```json
{
    "conversation_id": "string",
    "response": "string",
    "tool_calls": [{"name": "string", "input": {}}]
}
```

**GET `/c/{conversation_id}`** handler:
1. Verify conversation belongs to user
2. Load conversation history from checkpointer: `await checkpointer.aget({"configurable": {"thread_id": conversation_id}})`
3. Extract messages from the checkpoint state
4. Render chat.html with messages

**GET `/api/tools`** handler:
1. Return list of tool names and descriptions from the loaded MCP tools

### `templates/login.html` -- Login Page

Simple centered page with app title "Rhiza Agents" and a "Sign in with Keycloak" button linking to `/login`. Uses the same CSS as the chat page.

### `templates/chat.html` -- Chat UI

Adapt from sheerwater-chat's chat.html. Key differences:
- Title: "Rhiza Agents" instead of "Sheerwater Chat"
- Welcome message: "Ask questions about weather forecast models and data analysis."
- Remove settings modal (no per-user settings in phase 1)
- Keep: conversation sidebar, message display with markdown rendering, tool call display
- Remove: chart iframe rendering (not needed -- MCP tools return text data in this stack), rate limit bar, MCP version display
- Add: display tool call inputs as expandable JSON in the tool-call elements

Message rendering for server-rendered messages (on page load via Jinja):
- User messages: render the `content` field
- AI messages: render the `content` field with markdown
- Tool messages: skip (they are intermediate -- only show tool names as badges on the AI message that triggered them)

The template receives:
- `request`, `user_name`, `conversations` (list), `current_conversation` (dict or None), `messages` (list of dicts with `type`, `content`, `name`, `tool_calls` fields)

### `static/chat.js` -- Chat JavaScript

Adapt from sheerwater-chat's chat.js. Key pieces:
- `marked` + `highlight.js` for markdown rendering (same CDN imports)
- Form submit handler: POST to `/api/chat`, handle response
- Auto-resize textarea
- Enter to send, Shift+Enter for newline
- Loading state with "Thinking..." animation
- On new conversation: update URL to `/c/{id}` via `history.pushState`
- Render tool calls as small badges below assistant messages
- Render server-side messages (those with `needs-render` class) through marked on page load
- Remove: settings modal JS, rate limit display, chart URL handling

### `static/style.css` -- Dark Theme

Copy directly from sheerwater-chat's style.css. Changes:
- Remove: rate-limit styles, chart-container/chart-iframe styles (not needed in phase 1)
- Keep everything else: login page, sidebar, messages, input area, tool calls, markdown styles, modal base styles, loading animation

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

### Message Extraction from Checkpointer

When loading a conversation for display, get the checkpoint state and extract messages:

```python
state = await agent.aget_state({"configurable": {"thread_id": conversation_id}})
messages = state.values.get("messages", [])
```

Each message in the list is a LangChain message object. Convert to display format:
- `HumanMessage` -> `{"type": "human", "content": msg.content}`
- `AIMessage` -> `{"type": "ai", "content": msg.content, "tool_calls": [...]}`
- `ToolMessage` -> skip for display (tool results are shown as badges on the preceding AI message)

For tool calls on an `AIMessage`, access `msg.tool_calls` which is a list of dicts with `name`, `args`, `id` keys.

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
6. Agent calls the MCP tool, response appears with tool call badges
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
