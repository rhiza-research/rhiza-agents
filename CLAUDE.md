# rhiza-agents

Multi-agent chat platform built on LangChain Deep Agents and LangGraph.

## Before Starting Work

1. Read `docs/ARCHITECTURE.md` for full system overview
2. Read the relevant phase spec in `docs/specs/` for your task
3. Read the reference docs in `docs/reference/` as needed

## Development

### Local dev stack (full Docker)
```bash
docker compose up
```
This starts Keycloak (port 8180), sheerwater-mcp (port 8000), LangGraph Server (port 8123), and deep-agents-ui (port 3000).

### Rapid agent iteration
```bash
langgraph dev
```
Starts a local LangGraph Server with hot reload. Point deep-agents-ui at `http://localhost:8123`.

### Dependencies
```bash
uv sync                              # core deps
uv sync --extra sandbox              # + Daytona SDK
```

### Tests
```bash
uv run pytest
```

## Conventions

- Python 3.12+
- Package manager: `uv` (never pip)
- Linter: `ruff`
- Agent framework: `deepagents` (`create_deep_agent`)
- Frontend: deep-agents-ui (separate repo, Next.js fork)
- Auth: NextAuth.js + Keycloak OIDC (in the UI fork)

## Key Architecture Decisions

- **Deep Agents** provides the agent harness — planning, subagents, context management, file ops out of the box.
- **LangGraph Server** (self-hosted) provides the API layer — threads, runs, streaming, checkpointing. No custom FastAPI app.
- **deep-agents-ui** (forked) provides the frontend — chat, streaming, tool visualization, thread history. No custom templates/JS.
- **MCP tools** are loaded via `langchain-mcp-adapters` and passed to the agent.
- **Middleware** handles retry, context management, and tool call limits.

## File Structure

```
src/rhiza_agents/
  agent.py            # Deep agent definition (create_deep_agent)
  tools/
    mcp.py            # MCP tool loading
    sandbox.py        # Daytona sandbox tool
langgraph.json        # LangGraph Server agent configuration
```

## Phase Completion Process

After completing implementation for a phase, before considering the phase done:

1. **Update the phase spec** (`docs/specs/XX-*.md`) to reflect what was actually built.
2. **Update `docs/ARCHITECTURE.md`** with any new architectural patterns or constraints discovered.
3. **Commit the doc updates** as a separate commit from the implementation.

## Current Specs

| Phase | Spec | Status |
|-------|------|--------|
| 1 | `docs/specs/01-mvp-deep-agent.md` | MVP: Deep Agent + MCP Tools |
| 2 | `docs/specs/02-auth.md` | Auth: NextAuth.js + Keycloak |
| 3 | `docs/specs/03-sandbox-middleware.md` | Sandbox + Middleware |
| 4 | `docs/specs/04-production-deploy.md` | Production Deploy |
