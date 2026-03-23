# rhiza-agents Architecture

## Overview

rhiza-agents is a multi-agent chat platform built on LangGraph. Users log in, interact with a team of AI agents, and can customize agent behavior (prompts, tools, MCP servers, knowledge bases) through a JupyterLab-style panel UI. The system uses a supervisor agent that routes user messages to specialized sub-agents based on intent.

## System Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    Web Browser (Lumino)                    │
│  Chats │ Chat │ Activity │ Files │ FileViewer │ Config    │
│  (dockable, resizable, draggable panels)                  │
└─────────────────────────┬────────────────────────────────┘
                       │ HTTP / SSE
┌──────────────────────▼──────────────────────────────┐
│              FastAPI Application (app.py)             │
│                                                       │
│  routes/chat.py      SSE streaming + resume           │
│  routes/agents.py    Agent config CRUD                │
│  routes/mcp_servers.py  MCP server CRUD               │
│  routes/vectorstores.py Vector store CRUD             │
│  routes/conversations.py Messages + files API         │
│  routes/pages.py     HTML page routes                 │
│  deps.py             Shared dependencies (DI)         │
│  messages.py         Message processing + name res.   │
│  Auth: Keycloak OIDC via authlib                      │
└──┬──────────┬──────────┬──────────┬──────────────────┘
   │          │          │          │
   ▼          ▼          ▼          ▼
┌──────┐ ┌────────┐ ┌────────┐ ┌────────────┐
│ App  │ │LangGraph│ │ChromaDB│ │  LangGraph │
│  DB  │ │Checkpt. │ │VectorDB│ │   Graph    │
│SQLite│ │ SQLite  │ │        │ │  (dynamic) │
└──────┘ └────────┘ └────────┘ └──┬───┬───┬──┘
                                  │   │   │
                    ┌─────────────┘   │   └──────────┐
                    ▼                 ▼              ▼
             ┌────────────┐   ┌──────────┐   ┌──────────┐
             │ Supervisor │   │  Worker  │   │  Worker  │
             │   Agent    │   │  Agent   │   │  Agent   │
             │ (routes)   │   │  (data)  │   │  (code)  │
             └─────┬──────┘   └────┬─────┘   └────┬─────┘
                   │               │               │
          handoff tools     ┌──────┘          ┌────┘
                            ▼                 ▼
                ┌───────────────────┐  ┌──────────┐
                │ MCP Servers (SSE) │  │ Daytona  │
                │ System + User     │  │ Sandbox  │
                │ (per-user config) │  │ (hosted) │
                └───────────────────┘  └──────────┘
```

## Frontend Architecture

The frontend is a TypeScript application built with esbuild and Lumino (the layout framework from JupyterLab).

### Lumino Widgets

| Widget | Description | Default Position |
|--------|-------------|-----------------|
| `ConversationListWidget` | Chat history list, new chat button | Left |
| `ChatWidget` | Message display, input, SSE streaming | Center |
| `ActivityWidget` | Thinking, tool calls, tool results | Right |
| `FilesWidget` | File list from conversation state | Right (below Activity) |
| `FileViewerWidget` | Syntax-highlighted file content tab | Center (new tab) |
| `ConfigWidget` | Agent config, MCP servers, knowledge bases, settings | Center (tab) |

Panels are dockable — users can drag tabs to rearrange, resize splits, and close/reopen via the View menu. The layout uses `DockPanel` with explicit initial sizes via `restoreLayout()`.

### Build Chain

- Source: `frontend/src/**/*.ts`
- Bundler: esbuild (outputs `static/app.js` + `static/app.css`)
- Watch: `frontend/watch.mjs` (polling-based for Docker volume mount compatibility)
- Docker: `esbuild` service in docker-compose runs the watcher
- Theme: JupyterLab dark theme CSS variables in `static/theme.css`

## Technology Stack

| Component | Package | Purpose |
|-----------|---------|---------|
| Agent orchestration | `langgraph` | Graph-based agent state machine |
| Multi-agent routing | `langgraph-supervisor` | Supervisor + handoff pattern |
| LLM integration | `langchain-anthropic` | Claude model binding with extended thinking |
| MCP bridge | `langchain-mcp-adapters` | MCP tools → LangChain tools (SSE transport) |
| Chat persistence | `langgraph-checkpoint-sqlite` | Conversation state checkpointing |
| Context management | `SummarizationMiddleware` | Automatic conversation summarization |
| Sandbox | `daytona-sdk` | Hosted code execution |
| Vector store | `langchain-chroma` | In-process RAG |
| Web framework | `fastapi` | HTTP API with dependency injection |
| Auth | `authlib` | Keycloak OIDC |
| App database | `databases[aiosqlite]` | User configs, conversation metadata, MCP servers |
| Structured logging | `python-json-logger` | JSON chat event logs |
| Frontend layout | `@lumino/widgets` | Dockable panel layout (from JupyterLab) |
| Frontend bundler | `esbuild` | TypeScript → JS bundle |
| Markdown | `marked` + `highlight.js` | Rendering with syntax highlighting |
| Icons | `font-awesome` | Tab close icons (used by Lumino) |

## Agent Topology

### Supervisor Agent

The supervisor receives every user message and decides which sub-agent should handle it. It uses `create_supervisor()` from `langgraph-supervisor`, which automatically generates `transfer_to_<agent_name>` handoff tools.

The supervisor's system prompt is dynamically enhanced at graph build time with:
- **Agent tool assignments** — which agent has which tools
- **MCP server info** — connected server names and their tool lists

This allows the supervisor to answer questions about available tools and route correctly to agents with specific MCP tools.

Configuration:
- `output_mode="full_history"` — supervisor sees all sub-agent messages
- `add_handoff_back_messages=True` — supervisor knows when a sub-agent finishes

### Default Sub-Agents

1. **Data Analyst** (`data_analyst`)
   - Tools: Sheerwater MCP tools + any user-assigned MCP tools
   - Purpose: Answer questions about forecast models, run metrics, generate charts

2. **Code Runner** (`code_runner`)
   - Tools: Daytona sandbox (code execution, file I/O)
   - Purpose: Write and execute Python code for custom analysis

3. **Research Assistant** (`research_assistant`)
   - Tools: Vector store retrieval per attached collections
   - Purpose: Answer questions from uploaded documents/knowledge bases

### Agent Configuration

Each agent is defined by an `AgentConfig`:
```
id: str              # unique identifier
name: str            # display name
type: str            # "supervisor" or "worker"
system_prompt: str   # the agent's system prompt
model: str           # Claude model
tools: list[str]     # tool identifiers (e.g., ["mcp:sheerwater", "sandbox:daytona"])
vectorstore_ids: list[str]  # vector store collection IDs
enabled: bool        # whether this agent is active
```

**Defaults** are defined in code (`agents/registry.py`). **User overrides** are stored in the app database as JSON per `(user_id, agent_id)`. At graph build time, defaults are loaded and user overrides applied on top.

## MCP Server Integration

MCP servers come in two tiers:

- **System servers** — configured via environment variables, available to all users, seeded into the database at startup
- **User servers** — configured per-user via the Config UI, stored in `mcp_servers` table

Tool loading:
1. System MCP tools are loaded at startup and cached globally
2. User MCP tools are loaded on demand and cached per-server with the `_user_mcp_cache`
3. Tools are resolved per-agent via the `mcp:<server_id>` pattern in the `tools` list
4. The graph cache key includes MCP server IDs so it invalidates when tools change

The `/api/mcp-servers` endpoints provide CRUD + connectivity testing. The Config widget shows system servers as read-only and user servers with add/edit/test/delete.

## Data Flow

### Chat Message Flow (Streaming)

1. User sends message via `POST /api/chat/stream`
2. Server loads user's effective agent config (defaults + overrides)
3. User's MCP tools are loaded (system + per-user servers)
4. `agents/graph.py` builds or retrieves cached LangGraph graph
5. Supervisor prompt is enhanced with tool assignments and MCP server info
6. Graph is streamed via `graph.astream()` with `subgraphs=True`, `stream_mode=["messages", "updates", "custom"]`
7. Supervisor decides which agent to route to
8. Sub-agent executes with its tools (MCP calls, sandbox execution, RAG retrieval)
9. Tokens stream back as SSE events (`token`, `agent_start`, `tool_start`, `tool_end`, `done`)
10. All state persisted by LangGraph checkpointer
11. Frontend creates a new chat bubble per agent handoff, renders tokens with markdown

Page reloads use the `/api/conversations/{id}/messages` endpoint which loads from the LangGraph checkpointer via `process_messages()`. Both paths use `resolve_agent_name()` for consistent agent attribution.

### Agent Name Resolution

Agent names are resolved by `resolve_agent_name()` in `messages.py`, used by both streaming and refresh:
1. Try subgraph namespace (strip UUID suffixes from `ns` tuple)
2. Try node name directly
3. Fall back to provided default

The `build_name_mappings()` function creates the `agent_names` and `tool_to_agent` dicts from effective configs.

### Structured Chat Event Logging

When enabled, every chat event is logged as structured JSON to stdout via `chat_event_logger`:
- `graph_build` — agents, MCP servers, tool counts
- `user_message` — message content
- `agent_start` — which agent is active
- `agent_message` — accumulated agent response text
- `tool_start` / `tool_end` — tool name, args, output
- `interrupt` — HITL approval requests
- `error` / `done` — completion events

Each event includes `conversation_id` and `user_id` for filtering. Logging is configurable globally (`CHAT_EVENT_LOGGING` env var) and per-user (opt-in/opt-out setting).

## Persistence

### Two Storage Systems

1. **LangGraph Checkpointer** (SQLite)
   - Stores: full conversation state, all messages, tool calls, files
   - Keyed by: `thread_id` (= conversation UUID)
   - Source of truth for chat history

2. **App Database** (SQLite via `databases[aiosqlite]`)
   - Tables: `conversations`, `user_agent_configs`, `user_vectorstores`, `mcp_servers`, `user_settings`
   - Does NOT store messages (that's the checkpointer's job)

## Backend Module Structure

```
app.py              # FastAPI app creation, lifespan, shared state on app.state
deps.py             # Dependency injection: get_db(), require_auth(), get_mcp_tools_for_user()
messages.py         # resolve_agent_name(), process_messages(), extract_content_blocks()
logging_config.py   # setup_logging(), chat_event_logger
routes/
    chat.py         # SSE streaming (largest module — event generators, tool resolution)
    agents.py       # Agent config CRUD + tool-types endpoint
    mcp_servers.py  # MCP server CRUD + test connectivity
    vectorstores.py # Vector store CRUD + document upload
    conversations.py # List, delete, messages API, files API
    pages.py        # HTML page routes
    settings.py     # User settings API
```

Shared state (database, checkpointer, MCP tools, vectorstore manager) is initialized in the lifespan and stored on `app.state`. Route handlers access it via dependency injection from `deps.py`.

## Deployment

### Local Development
`podman compose up -d` starts Keycloak, sheerwater-mcp, esbuild watcher, and the app with hot reload.

### Production
GitOps: push to `main` → GitHub Actions builds image → pushes to GHCR → updates chart values → ArgoCD syncs to GKE.
