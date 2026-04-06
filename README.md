# rhiza-agents

Multi-agent chat platform built on [LangGraph](https://github.com/langchain-ai/langgraph). Users log in, interact with a team of AI agents backed by Claude, and can customize agent behavior — prompts, tools, MCP servers, and knowledge bases — through a JupyterLab-style panel layout.

## Architecture

A **supervisor agent** routes user messages to specialized **sub-agents**:

- **Data Analyst** — queries weather forecast benchmarking data via [Sheerwater](https://github.com/rhiza-research/sheerwater) MCP tools
- **Code Runner** — executes Python code in [Daytona](https://www.daytona.io/) sandboxes, writes files to a virtual filesystem
- **Research Assistant** — answers questions from uploaded documents via ChromaDB vector stores

Agent configurations (system prompts, tools, MCP servers, vector store links) are user-editable and stored per-user. Users can connect their own MCP servers and assign them to specific agents.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full system design.

## Features

- **Dockable panel layout** — Lumino-based (from JupyterLab) with draggable, resizable, closable panels for Chat, Activity, Files, Chats, and Config
- **SSE streaming** — real-time token streaming with per-agent chat bubbles and handoff tracking
- **File viewer** — agent-generated files open as syntax-highlighted tabs with markdown rendering toggle
- **User MCP servers** — add custom MCP servers via the UI, test connectivity, assign to agents
- **Knowledge bases** — upload documents (PDF, text, markdown), query via RAG retrieval tools
- **Code execution approval** — optional human-in-the-loop review before sandbox code runs
- **Extended thinking** — agent reasoning visible in the Activity panel
- **Structured logging** — JSON chat event logs with conversation/user IDs for debugging
- **JupyterLab dark theme** — themed with the official JupyterLab dark color palette

## Development

### Prerequisites

- [uv](https://docs.astral.sh/uv/)
- [Podman](https://podman.io/) or Docker with Compose
- [Node.js](https://nodejs.org/) 20+ (for local frontend builds)

### Local dev stack

```bash
podman compose up -d
```

Starts Keycloak (port 8180), sheerwater-mcp (port 8000), esbuild watcher, and the app (port 8080).

Login at http://localhost:8080 with `dev` / `dev`.

### Frontend

The frontend is TypeScript built with esbuild. The `esbuild` docker-compose service watches for changes and rebuilds automatically.

For local development without Docker:

```bash
cd frontend
npm install
npm run watch
```

### Running directly

```bash
uv sync
uv run rhiza-agents
```

### Tests

```bash
uv run pytest
```

### Langfuse MCP

The local Langfuse stack exposes an MCP endpoint at
`http://localhost:3000/api/public/mcp` for AI coding assistants to query
traces, prompts, and scores. It uses HTTP Basic auth with your Langfuse keys
— add this line to your `.envrc` once so any tool can pick up a base64-encoded
auth header:

```bash
export LANGFUSE_BASIC_AUTH=$(echo -n "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" | base64)
```

Run `direnv allow`, then wire `${LANGFUSE_BASIC_AUTH}` into your editor or
agent's MCP config under an `Authorization: Basic ${LANGFUSE_BASIC_AUTH}`
header. The exact mechanism is tool-specific; project-scoped MCP config files

## Project Structure

```
src/rhiza_agents/
    app.py              # FastAPI app, lifespan, router registration
    config.py           # Environment-based configuration
    deps.py             # Shared dependencies (get_db, require_auth, etc.)
    messages.py         # Message processing and agent name resolution
    logging_config.py   # Chat event logger setup
    auth.py             # Keycloak OIDC authentication
    routes/
        pages.py        # HTML page routes (/, /c/{id})
        chat.py         # SSE streaming (/api/chat/stream, /api/chat/resume)
        agents.py       # Agent config CRUD
        mcp_servers.py  # MCP server CRUD
        vectorstores.py # Vector store CRUD + document upload
        conversations.py # Conversation list, messages, files API
        settings.py     # User settings API
    agents/
        registry.py     # Default agent definitions + user override merging
        graph.py        # Dynamic LangGraph graph construction
        supervisor.py   # Supervisor agent setup
        tools/          # MCP, sandbox, file, vectordb tools
    db/
        sqlite.py       # SQLite database (conversations, configs, MCP servers, settings)
        models.py       # Pydantic models (AgentConfig)
    vectorstore/        # ChromaDB collection management
    templates/          # Jinja2 HTML templates
    static/             # CSS (style.css, theme.css)
frontend/
    src/
        app.ts          # Lumino layout, widgets, menu bar
        widgets/        # Chat, Activity, Files, FileViewer, Conversations, Config
        lib/            # Shared markdown renderer, SSE parser
    package.json        # esbuild, Lumino, highlight.js, marked, FontAwesome
    watch.mjs           # Polling file watcher for Docker volume mounts
```

## Build Phases

| Phase | Spec | Status |
|-------|------|--------|
| 1 | [01-mvp-single-agent.md](docs/specs/01-mvp-single-agent.md) | Complete |
| 2 | [02-multi-agent-supervisor.md](docs/specs/02-multi-agent-supervisor.md) | Complete |
| 3 | [03-user-editable-config.md](docs/specs/03-user-editable-config.md) | Complete |
| 4 | [04-sandbox-execution.md](docs/specs/04-sandbox-execution.md) | Complete |
| 5 | [05-vector-store.md](docs/specs/05-vector-store.md) | Complete |
| 6 | [06-streaming.md](docs/specs/06-streaming.md) | Complete |
| 7 | [07-production-deploy.md](docs/specs/07-production-deploy.md) | Complete |
| 8 | [08-context-management.md](docs/specs/08-context-management.md) | Complete |
| 9 | [09-file-viewer-and-execution-approval.md](docs/specs/09-file-viewer-and-execution-approval.md) | Complete |
| 10 | [10-extended-thinking.md](docs/specs/10-extended-thinking.md) | Complete |
| 11 | [11-lumino-panel-layout.md](docs/specs/11-lumino-panel-layout.md) | Complete |
| 12 | [12-user-mcp-servers.md](docs/specs/12-user-mcp-servers.md) | Complete |
| 13 | [13-main-py-refactor.md](docs/specs/13-main-py-refactor.md) | Complete |

## Stack

**Backend:**
- **LangGraph** — agent orchestration with supervisor pattern
- **Claude** (via langchain-anthropic) — LLM with extended thinking
- **langchain-mcp-adapters** — MCP tool integration (SSE transport)
- **FastAPI** — web framework with dependency injection
- **Keycloak** — OIDC authentication
- **SQLite** — app database + LangGraph checkpointer
- **ChromaDB** — vector store for RAG
- **Daytona** — sandboxed code execution

**Frontend:**
- **Lumino** — dockable panel layout (from JupyterLab)
- **TypeScript** — widget architecture
- **esbuild** — bundler with watch mode
- **marked** + **highlight.js** — markdown rendering with syntax highlighting
- **FontAwesome** — icons (used by Lumino)
