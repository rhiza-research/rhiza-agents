# Phase 13: Refactor main.py into Proper FastAPI Architecture

## Goal

Break the monolithic `main.py` (~1300 lines) into focused modules following FastAPI best practices. The result should be a codebase where each file has a single clear responsibility and shared state is managed through FastAPI's dependency injection rather than module-level globals.

## Problem

`main.py` currently contains all of the following in a single file:

- FastAPI app creation and lifespan
- Authentication helpers
- Agent name resolution and message processing
- Chat event logging setup
- Page routes
- Chat streaming API (the largest and most complex section)
- Agent config CRUD API
- MCP server CRUD API
- Vectorstore CRUD API
- File viewer API
- User settings API
- MCP tool loading
- Pydantic request/response models

These concerns change at different rates and for different reasons. Adding a new API endpoint requires reading through 1300 lines to find where it belongs. The streaming logic is interleaved with route definitions. Shared state is managed through module-level globals which makes testing difficult and the dependency graph invisible.

## Requirements

### Route Separation

Each API resource should have its own route module using FastAPI's `APIRouter`. The app entry point should only handle app creation, middleware, lifespan, and router registration.

### Shared State via app.state

Singleton resources (database, checkpointer, MCP tools, vectorstore manager) should be initialized in the lifespan context manager and stored on `app.state`. They should be accessed in route handlers via FastAPI dependency injection (`Depends()`), not through module-level globals.

### Message Processing as a Shared Module

The agent name resolution (`_resolve_agent_name`), message processing (`_process_messages`), and content extraction helpers are used by both the streaming path and the message loading path. These should live in a shared module that both paths import, ensuring they stay in sync.

### Logging as a Separate Concern

The chat event logging setup and the `_setup_logging` function should be extracted from the app initialization code.

### Preserve API Contracts

All endpoints must keep the same URLs, request formats, and response formats. This is a pure internal refactor — no external behavior changes.

## Constraints

- Follow FastAPI best practices for project structure (APIRouter, dependency injection, lifespan pattern)
- No import cycles between modules
- The streaming code in the chat routes is the most complex part and should be handled carefully — it references many shared objects
- Extract and test incrementally, not all at once

## Research

The following patterns were identified from production FastAPI projects:

- **FastAPI official template** (`full-stack-fastapi-template`): route-based organization with `api/routes/` and shared `api/deps.py`
- **fastapi-best-practices** (zhanymkanov): domain-based modules, each self-contained
- **Lifespan + app.state + Depends()**: the standard pattern for singleton resources in modern FastAPI
- **Anti-pattern**: bare module-level globals without lifespan management (what we currently have)

The route-based approach is more appropriate for this project since the API domains are mostly thin CRUD and the complex logic lives in the agent graph layer, not in the route handlers.

## Success Criteria

- `main.py` no longer exists (or is reduced to just the entry point)
- No module-level mutable globals for shared state
- Each route module can be read and understood independently
- The streaming chat code is isolated from CRUD endpoints
- All existing tests and manual testing still pass
- The agent name resolution logic is shared (not duplicated) between streaming and refresh paths
