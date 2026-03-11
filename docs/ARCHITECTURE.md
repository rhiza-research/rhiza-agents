# rhiza-agents Architecture

## Overview

rhiza-agents is a multi-agent chat platform built on LangChain Deep Agents and LangGraph. Users interact with a deep agent that has access to weather forecast benchmarking tools (via MCP), code execution (via Daytona sandboxes), and document retrieval. The system uses `create_deep_agent()` for agent orchestration with subagents for context isolation, LangGraph Server for the API layer, and deep-agents-ui for the frontend.

## System Architecture

```
┌──────────────────────────────────────────────────────┐
│                     Web Browser                       │
│          deep-agents-ui (Next.js + React)             │
│   Chat │ File Viewer │ Debug Mode │ Thread History    │
│   Auth: NextAuth.js + Keycloak OIDC                  │
└───────────────────────┬──────────────────────────────┘
                        │ LangGraph SDK (streaming)
┌───────────────────────▼──────────────────────────────┐
│              LangGraph Server (self-hosted)            │
│                                                        │
│   API: /threads, /runs, /assistants                    │
│   Runtime: inmem (no persistence, no license needed)   │
│   Agent: defined in langgraph.json                     │
└───────────┬───────────────────────────────────────────┘
            │
            ▼
┌───────────────────────────────────────────────────────┐
│              Deep Agent (create_deep_agent)             │
│                                                         │
│   Built-in: planning, file ops, context management      │
│   Middleware: summarization, retry, tool limits          │
│                                                         │
│   Tools:                                                │
│   ├── Sheerwater MCP tools (via langchain-mcp-adapters) │
│   └── Daytona sandbox (code execution)                  │
│                                                         │
│   Subagents:                                            │
│   ├── data_analyst (MCP tools, focused context)         │
│   └── code_runner (sandbox tool, focused context)       │
└──────┬──────────────────────┬────────────────────────┘
       │                      │
       ▼                      ▼
┌────────────┐         ┌──────────┐
│ Sheerwater │         │ Daytona  │
│ MCP Server │         │ Sandbox  │
│ (SSE)      │         │ (hosted) │
└────────────┘         └──────────┘
```

## Technology Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Agent framework | `deepagents` (`create_deep_agent`) | Agent harness with planning, subagents, context management |
| Agent orchestration | `langgraph` | Graph-based state machine, checkpointing, streaming |
| LLM integration | `langchain-anthropic` | Claude model binding |
| MCP bridge | `langchain-mcp-adapters` | MCP tools → LangChain tools |
| Middleware | `langchain` middleware | Summarization, retry, tool limits |
| Sandbox | `daytona-sdk` | Hosted code execution |
| API server | LangGraph Server (self-hosted) | Threads, runs, streaming API |
| Frontend | deep-agents-ui (Next.js/React) | Chat UI, file viewer, debug mode |
| Auth | NextAuth.js + Keycloak | OIDC authentication, user identity |
| Observability | LangSmith | Trace debugging |

## Agent Design

### Deep Agent

The main agent is created with `create_deep_agent()`, which provides out of the box:
- **Planning**: todo list tool for task decomposition
- **File operations**: read, write, edit, glob, grep for context management
- **Shell access**: command execution with sandboxing
- **Subagents**: `task` tool for delegating work with isolated context
- **Context management**: automatic summarization for long conversations

### Subagents

Subagents provide context isolation — they run in their own context window so the main agent doesn't accumulate tool call noise.

1. **Data Analyst** — Has sheerwater MCP tools. Handles questions about weather forecast models, metrics, comparisons, and visualizations.

2. **Code Runner** — Has the Daytona sandbox tool. Writes and executes Python code for custom analysis.

### MCP Integration

The sheerwater MCP server exposes 10+ tools for weather forecast benchmarking (model listing, metric evaluation, model comparison, chart generation). Tools are loaded via `langchain-mcp-adapters` and passed to the agent/subagents.

### Middleware

- **SummarizationMiddleware**: Automatically summarizes conversation history when approaching token limits
- **ModelRetryMiddleware**: Retries LLM calls on transient failures (rate limits, 5xx)
- **ToolCallLimitMiddleware**: Prevents runaway tool call loops

## Data Flow

### Chat Message Flow

1. User sends message in deep-agents-ui
2. UI calls LangGraph Server streaming API via `@langchain/langgraph-sdk`
3. LangGraph Server invokes the deep agent graph
4. Agent decides to handle directly or delegate to a subagent
5. Subagent executes with its tools (MCP calls, sandbox execution)
6. Response streams back through LangGraph Server to the UI
7. State is ephemeral (inmem runtime) — threads lost on restart

### Thread Management

LangGraph Server manages threads in memory. Threads are lost on container restart. The inmem runtime is used because the PostgreSQL-backed runtime requires a `LANGGRAPH_CLOUD_LICENSE_KEY`.

### Known Limitation: No Persistence

The inmem runtime means:
- Thread history does not survive restarts
- Old threads cannot be resumed after a container restart
- This is a fundamental limitation of the LangGraph Platform licensing model

To resolve this, the `langgraph_api` server layer would need to be replaced with a custom API server that manages its own PostgreSQL-backed state.

## Authentication

NextAuth.js with Keycloak OIDC provider, added to a fork of deep-agents-ui. The auth flow:

1. User visits the app → NextAuth.js checks session
2. No session → redirect to Keycloak login
3. Keycloak authenticates → callback to NextAuth.js
4. NextAuth.js creates session with user identity (sub, email, name)
5. User identity available in the app for per-user features

## Deployment

### Local Development

`docker compose up` starts:
- Keycloak (dev mode, port 8180)
- Sheerwater MCP server (port 8000)
- LangGraph Server (port 8123, inmem runtime)
- deep-agents-ui (port 3000)

For rapid iteration on agent code, `langgraph dev` can be used instead of the Docker-based LangGraph Server.

### Production

GitOps: push to `main` → GitHub Actions builds images → pushes to GHCR → ArgoCD syncs to GKE.

Two deployments in GKE:
1. LangGraph Server (with agent code) — inmem runtime (no persistence)
2. deep-agents-ui (Next.js)
