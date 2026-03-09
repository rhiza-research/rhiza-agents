# rhiza-agents

Multi-agent chat platform built on [LangGraph](https://github.com/langchain-ai/langgraph). Users log in, interact with a team of AI agents backed by Claude, and can customize agent behavior — prompts, tools, and knowledge bases — through the UI.

## Architecture

A **supervisor agent** routes user messages to specialized **sub-agents**:

- **Data Analyst** — queries weather forecast benchmarking data via [sheerwater](https://github.com/rhiza-research/sheerwater) MCP tools
- **Code Runner** — executes Python code in [Daytona](https://www.daytona.io/) sandboxes
- **Research Assistant** — answers questions from uploaded documents via ChromaDB vector stores

Agent configurations (system prompts, tools, vector store links) are user-editable and stored per-user, enabling rapid iteration without code deploys.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full system design.

## Development

### Prerequisites

- [uv](https://docs.astral.sh/uv/)
- [Docker](https://docs.docker.com/get-docker/) + Docker Compose

### Local dev stack

```bash
docker compose up
```

Starts Keycloak (port 8180), sheerwater-mcp (port 8000), and the app (port 8080).

Login at http://localhost:8080 with `dev` / `dev`.

### Running directly

```bash
uv sync
uv run rhiza-agents
```

### Tests

```bash
uv run pytest
```

## Build Phases

The project is built incrementally. Each phase has a self-contained spec in `docs/specs/`:

| Phase | Spec | Description |
|-------|------|-------------|
| 0 | — | Repo scaffolding + documentation (done) |
| 1 | [01-mvp-single-agent.md](docs/specs/01-mvp-single-agent.md) | Single agent with MCP tools, Keycloak auth, chat UI (done) |
| 2 | [02-multi-agent-supervisor.md](docs/specs/02-multi-agent-supervisor.md) | Supervisor routing to specialized sub-agents |
| 3 | [03-user-editable-config.md](docs/specs/03-user-editable-config.md) | User-editable agent configs via UI |
| 4 | [04-sandbox-execution.md](docs/specs/04-sandbox-execution.md) | Sandboxed Python execution via Daytona |
| 5 | [05-vector-store.md](docs/specs/05-vector-store.md) | ChromaDB vector stores for RAG |
| 6 | [06-streaming.md](docs/specs/06-streaming.md) | SSE streaming + real-time agent handoffs |
| 7 | [07-production-deploy.md](docs/specs/07-production-deploy.md) | CI/CD, Terraform, GKE deployment |

## Stack

- **LangGraph** — agent orchestration
- **Claude** (via langchain-anthropic) — LLM
- **langchain-mcp-adapters** — MCP tool integration
- **FastAPI** + Jinja2 — web framework
- **Keycloak** — authentication
- **SQLite** → PostgreSQL — persistence
- **ChromaDB** — vector store
- **Daytona** — sandboxed code execution
