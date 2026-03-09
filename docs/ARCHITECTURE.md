# rhiza-agents Architecture

## Overview

rhiza-agents is a multi-agent chat platform built on LangGraph. Users log in, interact with a team of AI agents, and can customize agent behavior (prompts, tools, knowledge bases) through the UI. The system uses a supervisor agent that routes user messages to specialized sub-agents based on intent.

## System Architecture

```
┌─────────────────────────────────────────────────────┐
│                    Web Browser                       │
│  Chat UI  │  Config Editor  │  Auth (Keycloak)       │
└────────────────────┬────────────────────────────────┘
                     │ HTTP
┌────────────────────▼────────────────────────────────┐
│              FastAPI Application                     │
│                                                      │
│  Routes: /api/chat, /api/agents, /config, etc.       │
│  Auth: Keycloak OIDC via authlib                     │
│  Session: itsdangerous signed cookies                │
└──┬──────────┬──────────┬──────────┬─────────────────┘
   │          │          │          │
   ▼          ▼          ▼          ▼
┌──────┐ ┌────────┐ ┌────────┐ ┌────────────┐
│ App  │ │LangGraph│ │ChromaDB│ │  LangGraph │
│  DB  │ │Checkpt. │ │VectorDB│ │   Graph    │
│SQLite│ │ SQLite  │ │on PVC  │ │  (dynamic) │
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
                     ┌────────────┐    ┌──────────┐
                     │ Sheerwater │    │ Daytona  │
                     │ MCP Server │    │ Sandbox  │
                     │ (SSE)      │    │ (hosted) │
                     └────────────┘    └──────────┘
```

## Technology Stack

| Component | Package | Version | Purpose |
|-----------|---------|---------|---------|
| Agent orchestration | `langgraph` | 1.0.10 | Graph-based agent state machine |
| Multi-agent routing | `langgraph-supervisor` | 0.0.31 | Supervisor + handoff pattern |
| LLM integration | `langchain-anthropic` | 1.3.4 | Claude model binding |
| MCP bridge | `langchain-mcp-adapters` | 0.2.1 | MCP tools → LangChain tools |
| Chat persistence | `langgraph-checkpoint-sqlite` | 3.0.3 | Conversation state checkpointing |
| Sandbox | `daytona-sdk` | 0.149.0 | Hosted code execution |
| Vector store | `langchain-chroma` | 1.1.0 | In-process RAG |
| Observability | `langsmith` | (latest) | Trace debugging (free tier) |
| Web framework | `fastapi` + `jinja2` | (latest) | HTTP API + server-rendered UI |
| Auth | `authlib` | (latest) | Keycloak OIDC |
| App database | `databases[aiosqlite]` | (latest) | User configs, conversation metadata |

## Agent Topology

### Supervisor Agent

The supervisor receives every user message and decides which sub-agent should handle it. It uses `create_supervisor()` from `langgraph-supervisor`, which automatically generates `transfer_to_<agent_name>` handoff tools.

Configuration:
- `output_mode="full_history"` — supervisor sees all sub-agent messages
- `add_handoff_back_messages=True` — supervisor knows when a sub-agent finishes

The supervisor does NOT have direct access to data tools — it only routes.

### Default Sub-Agents

1. **Data Analyst** (`data_analyst`)
   - Tools: All sheerwater MCP tools (discovery, evaluation, visualization, data extraction)
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
id: str              # unique identifier (e.g., "data_analyst")
name: str            # display name (e.g., "Data Analyst")
type: str            # "supervisor" or "worker"
system_prompt: str   # the agent's system prompt
model: str           # Claude model (e.g., "claude-sonnet-4-20250514")
tools: list[str]     # tool identifiers (e.g., ["mcp:sheerwater", "sandbox:daytona"])
vectorstore_ids: list[str]  # vector store collection IDs
enabled: bool        # whether this agent is active
```

**Defaults** are defined in code (`agents/registry.py`). **User overrides** are stored in the app database as JSON per `(user_id, agent_id)`. At graph build time, defaults are loaded and user overrides applied on top.

## Data Flow

### Chat Message Flow

1. User sends message via `POST /api/chat`
2. Server loads user's effective agent config (defaults + overrides)
3. `agents/graph.py` builds or retrieves cached LangGraph graph
4. Graph is invoked with `{"messages": [user_message]}` and `thread_id` = conversation UUID
5. Supervisor decides which agent to route to
6. Sub-agent executes with its tools (MCP calls, sandbox execution, RAG retrieval)
7. Response flows back through supervisor to user
8. All state persisted by LangGraph checkpointer

### Config Change Flow

1. User edits agent config in Config Editor UI
2. `PUT /api/agents/{agent_id}` saves override to `user_agent_configs` table
3. Graph cache for this user is invalidated
4. Next chat message triggers graph rebuild with new config

## Persistence

### Two Storage Systems

1. **LangGraph Checkpointer** (SQLite → Postgres later)
   - Stores: full conversation state, all messages, tool calls, intermediate agent states
   - Keyed by: `thread_id` (= conversation UUID)
   - This is the source of truth for chat history

2. **App Database** (SQLite via `databases[aiosqlite]` → Postgres later)
   - Stores: conversation metadata, user agent configs, vector store registrations, settings
   - Does NOT store messages (that's the checkpointer's job)

### Database Schema

See `docs/reference/database-schema.md` for full schema.

## MCP Integration

The sheerwater MCP server runs in GKE at `sheerwater-mcp` namespace, port 8000, SSE transport. It exposes 10+ tools for weather forecast benchmarking.

`langchain-mcp-adapters` (`MultiServerMCPClient`) converts MCP tools to LangChain-compatible tools at startup. Tools are cached and refreshed on reload.

See `docs/reference/mcp-integration.md` for details.

## Sandbox Integration

Daytona SDK provides hosted code execution sandboxes. One sandbox per conversation with idle timeout. The tool accepts Python code and returns stdout, stderr, and exit code.

See `docs/reference/daytona-integration.md` for details.

## Vector Store Integration

ChromaDB runs in-process with persistent storage on the PVC. Each user can create vector store collections and attach them to agents. The retrieval tool queries the collection and returns relevant document chunks.

## Authentication

Keycloak OIDC via authlib, same pattern as sheerwater-chat. Dual URL strategy (internal for backend → Keycloak, public for browser → Keycloak).

See `docs/reference/existing-code.md` for the auth pattern to reuse.

## Deployment

### Local Development
`docker compose up` starts Keycloak, sheerwater-mcp, and the app with hot reload.

### Production
GitOps: push to `main` → GitHub Actions builds image → pushes to GHCR → updates chart values → force-pushes `deploy` branch → ArgoCD syncs to GKE.

Helm chart: Recreate strategy (SQLite), PVC for data, ClusterIP service, nginx ingress with TLS.
