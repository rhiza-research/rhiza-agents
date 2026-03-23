# Phase 12: User-Configurable MCP Servers

## Goal

Users can add their own MCP servers via the UI, making those tools available to their agents. System-level MCP servers (like Sheerwater) remain available to all users. Each user controls which MCP servers are connected to which agents.

## Prerequisites

Phase 11 (Lumino panel layout) complete. Existing MCP tool loading working.

## Problem

MCP servers are currently configured as a single environment variable (`MCP_SERVER_URL`) loaded once at server startup. All users share the same set of MCP tools. There's no way for a user to:

1. Connect their own MCP servers (e.g., a local database MCP, a custom API MCP)
2. Control which agents have access to which MCP tools
3. See what MCP tools are available or test connectivity

## Design

### Two Tiers of MCP Servers

**System MCP servers** are configured via environment variables and available to all users. These are managed by the admin deploying the application. The existing `MCP_SERVER_URL` becomes one entry in a list of system servers.

**User MCP servers** are configured per-user via the UI and stored in the database. Each user can add, remove, and configure their own MCP servers. These tools are only available to that user's agents.

### Database Schema

```sql
CREATE TABLE mcp_servers (
    id TEXT PRIMARY KEY,
    user_id TEXT,           -- NULL for system-level servers
    name TEXT NOT NULL,     -- Display name (e.g., "My Database", "GitHub")
    url TEXT NOT NULL,      -- SSE endpoint URL
    transport TEXT NOT NULL DEFAULT 'sse',  -- 'sse' or 'stdio'
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_mcp_servers_user_id ON mcp_servers(user_id);
```

System servers have `user_id = NULL`. User servers have the user's ID. This lets us query both with `WHERE user_id IS NULL OR user_id = :user_id`.

### API Endpoints

```
GET    /api/mcp-servers          -- List all servers (system + user's own)
POST   /api/mcp-servers          -- Add a user MCP server
PUT    /api/mcp-servers/:id      -- Update a user MCP server
DELETE /api/mcp-servers/:id      -- Remove a user MCP server (user-owned only)
POST   /api/mcp-servers/:id/test -- Test connectivity, return tool list
```

The `GET` response distinguishes system vs user servers:

```json
{
    "servers": [
        {
            "id": "sheerwater",
            "name": "Sheerwater",
            "url": "http://sheerwater-mcp:8000/sse",
            "transport": "sse",
            "enabled": true,
            "system": true,
            "tool_count": 15
        },
        {
            "id": "user-abc123",
            "name": "My Database",
            "url": "http://localhost:3001/sse",
            "transport": "sse",
            "enabled": true,
            "system": false,
            "tool_count": 4
        }
    ]
}
```

### Tool Loading

Currently tools are loaded once at startup:

```python
# Current approach
mcp_tools = await load_mcp_tools(config.mcp_server_url)
```

New approach — tools are loaded per-user when building their agent graph:

```python
async def load_tools_for_user(user_id: str) -> list:
    # System MCP tools (cached globally, loaded at startup)
    tools = list(system_mcp_tools)

    # User MCP tools (cached per-user, loaded on demand)
    user_servers = await db.get_mcp_servers(user_id)
    for server in user_servers:
        if server.enabled:
            user_tools = await get_or_load_mcp_tools(server.url, server.id)
            tools.extend(user_tools)

    return tools
```

MCP tool loading is expensive (connects to the server, lists tools). We cache loaded tools with a TTL and invalidate when the user changes their MCP config.

### Tool-to-Agent Assignment

The existing `tools` field on `AgentConfig` uses string identifiers like `"mcp:sheerwater"` and `"sandbox:daytona"`. For user MCP servers, we use `"mcp:<server_id>"`:

```json
{
    "id": "data_analyst",
    "tools": ["mcp:sheerwater", "mcp:user-abc123"]
}
```

The agent config UI already has tool checkboxes. We extend the `/api/tool-types` endpoint to include user MCP servers as available tool types.

### Config UI

The Config widget gets a new "MCP Servers" section (alongside Agents, Knowledge Bases, and Settings):

```
MCP Servers
├── Sheerwater          SYSTEM    15 tools    [connected]
├── My Database         USER      4 tools     [connected]    [×]
└── + Add MCP Server
```

System servers are shown but can't be removed. User servers can be added, edited, tested, and removed.

**Add MCP Server form:**
- Name (display name)
- URL (SSE endpoint)
- Transport (SSE / stdio) — SSE for remote, stdio for local
- [Test Connection] button — connects, lists tools, shows count

### System MCP Server Configuration

System servers are configured via environment variables, loaded at startup, and seeded into the database with `user_id = NULL`:

```
MCP_SERVERS=sheerwater:http://sheerwater-mcp:8000/sse,other:http://other:9000/sse
```

Or keep the existing `MCP_SERVER_URL` for backwards compatibility and add `MCP_SERVERS` for multiple:

```python
# config.py
mcp_server_url: str  # Legacy single server (becomes "sheerwater" system server)
mcp_servers: str     # Comma-separated name:url pairs for additional system servers
```

On startup, system servers are synced to the database (insert if missing, update URL if changed, don't remove if env var removed — admin does that manually).

### Graph Cache Invalidation

The graph cache key currently includes agent configs. With user MCP servers, the cache key must also include the user's MCP server list. When a user adds/removes/changes an MCP server, their cached graph is invalidated.

```python
def _config_hash(configs, mcp_server_ids):
    data = json.dumps({
        "configs": [c.model_dump() for c in configs],
        "mcp_servers": sorted(mcp_server_ids),
    }, sort_keys=True)
    return hashlib.sha256(data.encode()).hexdigest()
```

### Error Handling

MCP servers may be unreachable. The tool loading must handle:

- **Connection timeout** — skip the server, log a warning, show "disconnected" in UI
- **Server goes down mid-session** — tool calls fail with a clear error message
- **Invalid URL** — catch during "Test Connection", don't save if it fails
- **Duplicate tool names** — if two MCP servers expose tools with the same name, prefix with server name (e.g., `sheerwater:list_metrics` vs `myserver:list_metrics`)

## Implementation Plan

### Step 1: Database + API

- Add `mcp_servers` table to sqlite.py
- Add CRUD endpoints to main.py
- Add test connectivity endpoint
- Seed system servers from env vars on startup

### Step 2: Per-User Tool Loading

- Refactor `load_mcp_tools` to work per-server instead of globally
- Add caching layer for loaded tools (per server URL, with TTL)
- Update `_resolve_tools` in graph.py to load user MCP tools
- Update graph cache key to include MCP server IDs

### Step 3: Agent Config Integration

- Update `/api/tool-types` to include user MCP servers
- Update agent config tool checkboxes to show MCP servers
- Handle `"mcp:<server_id>"` tool identifiers in `_resolve_tools`

### Step 4: Config UI

- Add "MCP Servers" section to ConfigWidget
- Add/remove/test server UI
- Show system vs user distinction
- Show connected/disconnected status and tool count

## What Doesn't Change

- System MCP servers still work via env vars
- Existing agent configs with `"mcp:sheerwater"` continue to work
- The supervisor graph architecture is unchanged
- MCP tool protocol (SSE transport) is unchanged
