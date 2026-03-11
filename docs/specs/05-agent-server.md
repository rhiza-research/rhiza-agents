# Phase 5: Open Source Agent Server (langgraph-api Replacement)

## Goal

Replace the proprietary `langgraph-api` Docker image with an open-source FastAPI server that implements the Agent Protocol API. This eliminates the LangGraph Platform license requirement while keeping `create_deep_agent()` and the entire deepagents framework unchanged. The server manages its own PostgreSQL-backed persistence, enabling production-ready thread management.

## Why

The `langgraph-api` server requires a `LANGGRAPH_CLOUD_LICENSE_KEY` for PostgreSQL-backed persistence. Without it, only the `inmem` runtime is available — threads are lost on restart. Even the "standalone self-hosted" deployment requires a LangSmith API key that phones home at startup. This is a hard blocker for production.

The agent code (`create_deep_agent()` → `CompiledStateGraph`) is fully open source. Only the HTTP server layer is proprietary. By replacing that layer, we get full persistence with no license dependencies.

## What You're Building

A FastAPI application that:

1. Implements the Agent Protocol OpenAPI spec (the subset deep-agents-ui actually uses)
2. Invokes the `CompiledStateGraph` from `create_deep_agent()` directly
3. Manages threads and checkpoints in PostgreSQL (using langgraph's open-source `AsyncPostgresSaver`)
4. Streams responses via SSE in the format `@langchain/langgraph-sdk` expects
5. Serves as a drop-in replacement — deep-agents-ui connects to it with zero changes

## What You're NOT Building

- No custom agent framework — `create_deep_agent()` and deepagents are used as-is
- No custom UI — deep-agents-ui works unchanged via `@langchain/langgraph-sdk`
- No custom checkpointer — use langgraph's open-source `AsyncPostgresSaver`
- No crons, no batch runs, no store API — deep-agents-ui doesn't use them

## Reference

- **Agent Protocol OpenAPI spec**: https://langchain-ai.github.io/agent-protocol/openapi.json
- **Agent Protocol docs**: https://langchain-ai.github.io/agent-protocol/api.html
- **Agent Protocol repo**: https://github.com/langchain-ai/agent-protocol
- LangGraph Platform implements a "superset" of this spec. We implement the base spec.

## Endpoints to Implement

The deep-agents-ui audit identified exactly 9 endpoints that are actually called:

### Assistants

| # | Method | Path | Purpose |
|---|--------|------|---------|
| 1 | GET | `/assistants/{id}` | Get assistant by UUID |
| 2 | POST | `/assistants/search` | Find assistant by graph name |

Assistants are static — they map to the compiled graph. No database needed. Return hardcoded metadata for the single agent graph.

### Threads

| # | Method | Path | Purpose |
|---|--------|------|---------|
| 3 | POST | `/threads` | Create a new thread |
| 4 | POST | `/threads/search` | List threads (sidebar pagination) |
| 5 | POST | `/threads/{id}/state` | Update thread state (files key) |
| 6 | POST | `/threads/{id}/history` | Fetch state history for a thread |

Threads are persisted in PostgreSQL. Use `AsyncPostgresSaver` for checkpointing.

### Runs

| # | Method | Path | Purpose |
|---|--------|------|---------|
| 7 | POST | `/threads/{id}/runs/stream` | Create and stream a run (SSE) |
| 8 | POST | `/threads/{id}/runs/{id}/cancel` | Cancel a running run |

### GenUI

| # | Method | Path | Purpose |
|---|--------|------|---------|
| 9 | POST | `/ui/{assistantId}` | Serve UI component definitions for tool rendering |

This replaces the hardcoded plotly iframe logic in ToolCallBox.tsx. Tools define their own UI components on the server, and `LoadExternalComponent` in the frontend renders them automatically. New tools can define rendering without frontend changes.

## SSE Streaming Contract

The hardest part. The `@langchain/langgraph-sdk` client expects SSE events in a specific format.

### Stream modes used by deep-agents-ui (via `useStream` hook):

The `useStream` hook negotiates stream modes automatically. Key events:

| SSE `event` field | `data` shape | When |
|-------------------|-------------|------|
| `metadata` | `{ run_id, thread_id }` | First event, always |
| `values` | Full state after each step | When `stream_mode` includes `"values"` |
| `updates` | `{ [nodeName]: updateData }` | When `stream_mode` includes `"updates"` |
| `messages/partial` | Partial message chunks | Token-by-token streaming |
| `messages/complete` | Complete message | End of message |
| `error` | `{ error, message }` | On error |

### Response headers:

- `Content-Type: text/event-stream`
- `Content-Location: /threads/{thread_id}/runs/{run_id}` — the SDK parses this to extract the run ID

### Reconnection:

Client sends `Last-Event-ID` header. Server replays missed events from that point.

## Architecture

```
deep-agents-ui (unchanged)
    │
    │ @langchain/langgraph-sdk (unchanged)
    │
    ▼
┌─────────────────────────────────┐
│   Agent Server (FastAPI)         │
│                                  │
│   /assistants/* → static config  │
│   /threads/*    → PostgreSQL     │
│   /runs/stream  → graph.astream()│
│   /ui/*         → GenUI server   │
│                                  │
│   Checkpointer: AsyncPostgresSaver│
│   Graph: create_deep_agent()     │
└──────┬──────────────┬────────────┘
       │              │
       ▼              ▼
   PostgreSQL    CompiledStateGraph
   (threads,     (deepagents, MCP
    checkpoints)  tools, sandbox)
```

## Key Implementation Details

### Graph invocation

`create_deep_agent()` returns a `CompiledStateGraph`. Invoke it with:

```python
async for event in graph.astream(input, config, stream_mode=[...]):
    # convert to SSE event and yield
```

The `config` dict must include the checkpointer and thread_id:

```python
config = {
    "configurable": {
        "thread_id": thread_id,
    }
}
```

### Checkpointing

Use `langgraph-checkpoint-postgres` (open source, MIT licensed):

```python
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

checkpointer = AsyncPostgresSaver.from_conn_string(database_url)
await checkpointer.setup()  # creates tables if needed
```

Pass the checkpointer when compiling the graph, or set it on the compiled graph.

### Thread management

Threads table in PostgreSQL:

- `thread_id` (UUID, primary key)
- `created_at`, `updated_at` (timestamps)
- `metadata` (JSONB)
- `status` (idle | busy | interrupted | error)

The checkpointer handles state/checkpoint storage separately.

### Run management

Runs are ephemeral — tracked in memory while executing. The runs table is optional for MVP but needed for:
- Cancel support (need to find the running task by run_id)
- Status queries

### GenUI component serving

The `/ui/{assistantId}` endpoint serves React component definitions. Components are defined alongside tools on the server and bundled/served as HTML snippets that `LoadExternalComponent` renders.

## Acceptance Criteria

1. `docker compose up` starts the agent server with PostgreSQL (no `langgraph-api` image, no license key)
2. deep-agents-ui connects and works with zero code changes (same SDK, same endpoints)
3. Threads persist across server restarts
4. Thread history appears in the UI sidebar and survives restarts
5. Chat works end-to-end: messages, tool calls, streaming
6. MCP tools work (sheerwater forecast benchmarking)
7. Plotly charts render via GenUI (LoadExternalComponent) instead of hardcoded iframe logic
8. Run cancellation works
9. No LangSmith API key or LangGraph license key required at any point

## What NOT to Do

- Do not modify `create_deep_agent()` or any deepagents internals
- Do not modify deep-agents-ui — if the SDK client can't connect, the server API is wrong
- Do not implement endpoints that deep-agents-ui doesn't use (crons, store, batch runs)
- Do not build a custom checkpointer — use `AsyncPostgresSaver`
- Do not build custom streaming — use `graph.astream()` and format the SSE events correctly
- Do not add authentication to the server in this phase — auth is handled by deep-agents-ui (Phase 2)
