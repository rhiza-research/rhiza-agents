# rhiza-agents

Multi-agent chat platform built on [LangChain Deep Agents](https://github.com/langchain-ai/deepagents) and [LangGraph](https://github.com/langchain-ai/langgraph).

## Overview

A deep agent with sheerwater MCP tools for weather forecast analysis, served by LangGraph Server (self-hosted), with [deep-agents-ui](https://github.com/langchain-ai/deep-agents-ui) as the frontend. Authentication via NextAuth.js + Keycloak OIDC. Sandboxed code execution via Daytona.

## Quick Start

### Full stack (Docker Compose)

```bash
docker compose up
```

Starts Keycloak (port 8180), sheerwater-mcp (port 8000), LangGraph Server (port 8123), and deep-agents-ui (port 3000).

### Rapid agent iteration

```bash
langgraph dev
```

Starts a local LangGraph Server with hot reload. Point deep-agents-ui at `http://localhost:8123`.

### Dependencies

```bash
uv sync                  # core deps
uv sync --extra sandbox  # + Daytona SDK
```

### Tests

```bash
uv run pytest
```

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full system overview and [docs/specs/](docs/specs/) for phase specs.

## Project Structure

```
src/rhiza_agents/
  agent.py            # Deep agent definition (create_deep_agent)
  tools/
    mcp.py            # MCP tool loading
    sandbox.py        # Daytona sandbox tool
langgraph.json        # LangGraph Server agent configuration
docker-compose.yml    # Full local dev stack
chart/                # Helm chart for production deployment
```
