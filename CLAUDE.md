# rhiza-agents

Multi-agent chat platform built on LangGraph.

## Before Starting Work

1. Read `docs/ARCHITECTURE.md` for full system overview
2. Read the relevant spec issue on GitHub for your task (see Specs section below)
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
      files.py      # File write/run tools (state-based virtual filesystem)
      vectordb.py   # RAG retrieval tool factory
  vectorstore/
    manager.py      # ChromaDB collection management
  templates/        # Jinja2 HTML templates
  static/           # CSS, JS
```

## Spec Completion Process

After completing implementation for a spec, before considering it done:

1. **Update the spec issue** to reflect what was actually built. Document deviations from the original spec: workarounds, changed approaches, discovered constraints.
2. **Update other open spec issues** that reference changed interfaces. Specs are used as implementation guides, so they must reference the correct current APIs.
3. **Update `docs/ARCHITECTURE.md`** with any new architectural patterns, data flows, or constraints discovered during implementation.

The goal is that specs always reflect the current state of the code, not the original plan.

## Specs

Specs live as GitHub issues with the `spec` label. Use `gh issue list --label spec` to see all specs. Use the `/spec` skill to propose, approve, implement, and update specs.

### Completed

| Issue | Title |
|-------|-------|
| [#1](https://github.com/rhiza-research/rhiza-agents/issues/1) | MVP — Single Agent with MCP Tools |
| [#4](https://github.com/rhiza-research/rhiza-agents/issues/4) | Multi-Agent with Supervisor |
| [#8](https://github.com/rhiza-research/rhiza-agents/issues/8) | User-Editable Config |
| [#12](https://github.com/rhiza-research/rhiza-agents/issues/12) | Sandboxed Code Execution |
| [#14](https://github.com/rhiza-research/rhiza-agents/issues/14) | Vector Store Integration |
| [#5](https://github.com/rhiza-research/rhiza-agents/issues/5) | Streaming |
| [#9](https://github.com/rhiza-research/rhiza-agents/issues/9) | Production Deployment |
| [#11](https://github.com/rhiza-research/rhiza-agents/issues/11) | Context Management |
| [#13](https://github.com/rhiza-research/rhiza-agents/issues/13) | File Viewer and Code Execution Approval |
| [#2](https://github.com/rhiza-research/rhiza-agents/issues/2) | Extended Thinking |
| [#6](https://github.com/rhiza-research/rhiza-agents/issues/6) | Lumino Panel Layout |
| [#7](https://github.com/rhiza-research/rhiza-agents/issues/7) | User-Configurable MCP Servers |
| [#10](https://github.com/rhiza-research/rhiza-agents/issues/10) | Refactor main.py |

### Proposals

| Issue | Title |
|-------|-------|
| [#3](https://github.com/rhiza-research/rhiza-agents/issues/3) | Agent Skills |
| [#15](https://github.com/rhiza-research/rhiza-agents/issues/15) | Live Dashboards via MCP Apps |
| [#16](https://github.com/rhiza-research/rhiza-agents/issues/16) | MCP Apps — Interactive UI from MCP Servers |
| [#17](https://github.com/rhiza-research/rhiza-agents/issues/17) | Workflow Editor via MCP App |
| [#18](https://github.com/rhiza-research/rhiza-agents/issues/18) | MCP Server Authentication |
| [#19](https://github.com/rhiza-research/rhiza-agents/issues/19) | Scheduled Tasks |

## Key Principles

- **Never write custom code when a built-in exists.** Always check `langchain` middleware, `langgraph` primitives, and community plugins first. See `docs/reference/langchain-docs-summary.md` for the full inventory.
- **Security must be structural, not prompt-based.** Use HITL middleware for execution approval, not prompt instructions. LLMs cannot be trusted to enforce security.

## Reference Files (in sibling repos)

These files contain patterns to reuse:
- `../sheerwater-chat/src/sheerwater_chat/auth.py` — Keycloak OIDC pattern
- `../sheerwater-chat/src/sheerwater_chat/main.py` — FastAPI app structure
- `../sheerwater-chat/src/sheerwater_chat/database.py` — Database abstraction
- `../sheerwater-chat/src/sheerwater_chat/templates/chat.html` — Chat UI
- `../sheerwater-chat/src/sheerwater_chat/static/` — Frontend assets
