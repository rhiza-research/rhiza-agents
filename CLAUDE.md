# rhiza-agents

Multi-agent chat platform built on LangGraph.

## Before Starting Work

1. Read `docs/ARCHITECTURE.md` for full system overview
2. Read the relevant phase spec in `docs/specs/` for your task
3. Read the reference docs in `docs/reference/` as needed

## Development

### Local dev stack
```bash
docker compose up
```
This starts Keycloak (port 8180), sheerwater-mcp (port 8000), and the app (port 8080).

Login: http://localhost:8080 with dev/dev

### Dependencies
```bash
uv sync                              # core deps
uv sync --extra sandbox              # + Daytona SDK
uv sync --extra vectorstore          # + ChromaDB
uv sync --extra sandbox --extra vectorstore  # all extras
```

### Running directly (without Docker)
```bash
uv run rhiza-agents
```

### Tests
```bash
uv run pytest
```

## Conventions

- Python 3.12+
- Package manager: `uv` (never pip)
- Linter: `ruff`
- Async: use `async/await` throughout (FastAPI is async)
- Database: raw SQL via `databases` library, no ORM
- Templates: Jinja2, server-rendered
- JS: vanilla JS, no frameworks, ES modules

## Key Architecture Decisions

- **LangGraph checkpointer** stores all chat messages. The app DB only stores metadata (conversation title, user configs).
- **Graphs are built dynamically** per-user based on their agent config (defaults + overrides).
- **MCP tools** are loaded via `langchain-mcp-adapters` at startup and cached.
- **Agent configs** are Pydantic models. Defaults in code, user overrides in DB as JSON.

## File Structure

```
src/rhiza_agents/
  main.py           # FastAPI app, lifespan, all routes
  config.py         # Environment-based configuration
  auth.py           # Keycloak OIDC authentication
  db/
    base.py         # Abstract database interface
    sqlite.py       # SQLite implementation
    models.py       # Pydantic models (AgentConfig, etc.)
  agents/
    registry.py     # Default agent definitions + user override merging
    graph.py        # Dynamic LangGraph graph construction
    supervisor.py   # Supervisor agent setup
    tools/
      mcp.py        # MCP tool loading
      sandbox.py    # Daytona sandbox tool
      vectordb.py   # RAG retrieval tool factory
  vectorstore/
    manager.py      # ChromaDB collection management
  templates/        # Jinja2 HTML templates
  static/           # CSS, JS
```

## Phase Completion Process

After completing implementation for a phase, before considering the phase done:

1. **Update the phase spec** (`docs/specs/XX-*.md`) to reflect what was actually built. Document deviations from the original spec: workarounds, changed approaches, discovered constraints (e.g., checkpoint serialization losing `msg.name`).
2. **Update earlier phase specs** if the implementation changed shared interfaces (e.g., `_process_messages()` return format).
3. **Update future phase specs** that reference changed interfaces (e.g., function signatures, JS function names, API response formats). These specs are used as implementation guides, so they must reference the correct current APIs.
4. **Update `docs/ARCHITECTURE.md`** with any new architectural patterns, data flows, or constraints discovered during implementation.
5. **Commit the doc updates** as a separate commit from the implementation.

The goal is that specs always reflect the current state of the code, not the original plan. Future phases build on what was actually implemented, not what was originally specified.

## Reference Files (in sibling repos)

These files contain patterns to reuse:
- `../sheerwater-chat/src/sheerwater_chat/auth.py` — Keycloak OIDC pattern
- `../sheerwater-chat/src/sheerwater_chat/main.py` — FastAPI app structure
- `../sheerwater-chat/src/sheerwater_chat/database.py` — Database abstraction
- `../sheerwater-chat/src/sheerwater_chat/templates/chat.html` — Chat UI
- `../sheerwater-chat/src/sheerwater_chat/static/` — Frontend assets
