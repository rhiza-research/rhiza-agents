# Phase 1: MVP — Deep Agent with MCP Tools

## Goal

A working deep agent with sheerwater MCP tools, served by LangGraph Server, with deep-agents-ui as the frontend. The full stack runs locally via Docker Compose. Users can ask questions about weather forecast models and get answers with tool calls visible in the UI. No auth in this phase.

## What You're Building

1. A Python package that defines a deep agent via `create_deep_agent()` with sheerwater MCP tools
2. A `langgraph.json` that points LangGraph Server at the agent
3. A Docker Compose stack that runs everything together

## What You're NOT Building

- No custom FastAPI app
- No custom chat UI, templates, CSS, or JavaScript
- No custom streaming implementation
- No custom message processing or conversation management
- No auth (Phase 2)
- No sandbox execution (Phase 3)

## Key Packages

| Package | Purpose |
|---------|---------|
| `deepagents` | `create_deep_agent()` — agent harness |
| `langchain-anthropic` | Claude model binding |
| `langchain-mcp-adapters` | MCP tools → LangChain tools |
| `langgraph-cli` | Build + serve agent locally via `langgraph dev` |

## Implementation Details

### Python Package Structure

The package should define the agent graph and expose it for LangGraph Server. The entry point is a module-level compiled graph that LangGraph Server can import.

The agent needs:
- A system prompt tailored for weather/climate data analysis
- Sheerwater MCP tools loaded via `langchain-mcp-adapters`
- Claude as the model (`claude-sonnet-4-20250514`)

### MCP Tool Loading

Use `langchain-mcp-adapters` (`MultiServerMCPClient`) to connect to the sheerwater MCP server and get LangChain-compatible tools, same as before. The MCP server URL comes from an environment variable.

Important: `MultiServerMCPClient` is NOT an async context manager in recent versions. Call `await client.get_tools()` directly.

### langgraph.json

This file tells LangGraph Server where to find the agent graph. It points to the compiled graph object in your Python module.

### Docker Compose

Four services:

1. **keycloak**: `quay.io/keycloak/keycloak:25.0`, start-dev with realm import, port 8180 (needed for Phase 2, but start it now so the realm is ready)
2. **sheerwater-mcp**: `ghcr.io/rhiza-research/sheerwater/mcp:latest`, port 8000
3. **langgraph-server**: Built from the agent code, runs LangGraph Server with PostgreSQL + Redis
4. **deep-agents-ui**: `langchain-ai/deep-agents-ui` (upstream, no fork yet), port 3000, configured to point at the LangGraph Server

The LangGraph Server needs PostgreSQL and Redis. These can be separate services in Docker Compose or bundled (check LangGraph's self-hosted Docker Compose examples for the canonical setup).

### Alternative: `langgraph dev` for Rapid Iteration

For fast iteration on agent code without rebuilding Docker images, use `langgraph dev` which starts a local LangGraph server with hot reload. Point deep-agents-ui at `http://localhost:8123` (or whatever port langgraph dev uses).

Docker Compose is for the full integrated stack. `langgraph dev` is for rapid agent development.

## Reference

For `create_deep_agent()` API:
- [Deep Agents GitHub](https://github.com/langchain-ai/deepagents)
- The function returns a compiled LangGraph graph
- Accepts: `model`, `tools`, `system_prompt`, `subagents`, `middleware`

For LangGraph Server self-hosted:
- Check `langgraph-cli` docs for `langgraph dev` and `langgraph build`
- Check LangGraph's Docker Compose examples for the PostgreSQL + Redis setup

For MCP tools:
- `MultiServerMCPClient({"sheerwater": {"url": mcp_url, "transport": "sse"}})`
- `await client.get_tools()` returns list of LangChain tool objects

## Acceptance Criteria

1. `docker compose up` (or `langgraph dev` + separate deep-agents-ui) starts without errors
2. Open deep-agents-ui in browser, see the chat interface
3. Type "list available forecast models" → agent calls MCP tools, response appears with tool calls visible
4. Type "compare ECMWF and GFS on precipitation MAE" → agent calls compare_models tool, shows results
5. Thread history appears in the UI sidebar
6. Agent has planning capability (can break down complex requests)
7. Streaming works (tokens appear as they're generated)

## What NOT to Do

- Do not build a custom web framework or API layer — LangGraph Server provides the API
- Do not build custom streaming — deep-agents-ui handles this
- Do not add auth — that's Phase 2
- Do not add Daytona sandbox — that's Phase 3
- Do not fork deep-agents-ui — use upstream in this phase
