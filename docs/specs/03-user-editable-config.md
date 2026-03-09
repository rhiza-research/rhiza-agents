# Phase 3: User-Editable Config

## Goal

Users can edit agent system prompts, toggle tools on/off, enable/disable agents, add custom agents, and reset to defaults -- all through a config editor UI. Changes take effect on the next chat message by invalidating the cached graph for that user.

## Prerequisites

Phase 2 must be complete and working:
- Supervisor + sub-agent architecture with `AgentConfig` model
- `registry.py` with default configs, `graph.py` with dynamic graph building and caching
- Agent name badges in the chat UI

## Files to Create

```
src/rhiza_agents/templates/config_editor.html
src/rhiza_agents/static/config.js
```

## Files to Modify

```
src/rhiza_agents/db/sqlite.py
src/rhiza_agents/agents/registry.py
src/rhiza_agents/agents/supervisor.py
src/rhiza_agents/main.py
src/rhiza_agents/templates/chat.html
src/rhiza_agents/static/style.css
```

## Key APIs & Packages

```python
# Existing imports -- no new packages needed
from pydantic import BaseModel
from databases import Database as DatabaseConnection
import json
```

## Implementation Details

### Modifications to `db/sqlite.py` -- user_agent_configs Table

Add a new table to `_init_db()`:

```sql
CREATE TABLE IF NOT EXISTS user_agent_configs (
    user_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    config_json TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, agent_id)
);
```

Add CRUD methods to the `Database` class:

```python
async def get_user_agent_configs(self, user_id: str) -> list[dict]:
    """Get all agent config overrides for a user.

    Returns list of dicts with agent_id and the parsed config JSON.
    """

async def get_user_agent_config(self, user_id: str, agent_id: str) -> dict | None:
    """Get a single agent config override for a user."""

async def save_user_agent_config(self, user_id: str, agent_id: str, config: dict):
    """Save (insert or update) an agent config override.

    Uses INSERT ... ON CONFLICT ... DO UPDATE.
    """

async def delete_user_agent_config(self, user_id: str, agent_id: str):
    """Delete a single agent config override."""

async def delete_all_user_agent_configs(self, user_id: str):
    """Delete all agent config overrides for a user (reset to defaults)."""
```

The `config_json` column stores the full `AgentConfig` as a JSON string. This is a complete snapshot of the agent config, not a diff/patch. When a user saves config, the entire AgentConfig is serialized and stored.

### Modifications to `agents/registry.py` -- Config Merging

Add functions for loading effective configs:

```python
def merge_configs(
    defaults: list[AgentConfig],
    overrides: list[dict],
) -> list[AgentConfig]:
    """Merge user overrides on top of defaults.

    Args:
        defaults: Default agent configs from get_default_configs()
        overrides: List of dicts from the database (parsed config_json values)

    Returns:
        Final list of AgentConfig objects.

    Logic:
        1. Start with a copy of defaults, keyed by agent_id
        2. For each override:
           a. If the agent_id exists in defaults, replace with override
           b. If the agent_id doesn't exist in defaults, it's a custom agent -- add it
        3. Filter out agents with enabled=False
        4. Ensure exactly one supervisor exists (if user disabled it, re-enable it)
        5. Return the final list
    """
```

Key rules:
- The supervisor agent cannot be deleted or disabled. If a user tries, ignore that change.
- Users can disable any worker agent.
- Users can add new worker agents (with `type="worker"`). Users cannot add supervisors.
- If a user override sets `enabled=False` on a default agent, it is excluded from the graph but the override is kept in the DB (so they can re-enable it later).

### Modifications to `agents/supervisor.py`

Update `get_agent_graph` to accept user_id and load configs from the database:

```python
async def get_agent_graph(
    user_id: str,
    db: Database,
    mcp_tools: list,
    checkpointer,
) -> CompiledGraph:
    """Get the compiled agent graph for a user.

    Loads default configs, overlays user overrides from the database,
    and builds/caches the resulting graph.
    """
    defaults = get_default_configs()
    overrides_rows = await db.get_user_agent_configs(user_id)
    overrides = [json.loads(row["config_json"]) for row in overrides_rows]
    effective_configs = merge_configs(defaults, overrides)
    return await get_or_build_graph(effective_configs, mcp_tools, checkpointer)
```

### Modifications to `main.py`

**New page route:**

```
GET /config  (requires auth)
```

Renders `config_editor.html` with the user's effective agent configs.

**New API routes:**

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents` | Get effective agent configs for the current user |
| PUT | `/api/agents/{agent_id}` | Update (or create override for) an agent config |
| POST | `/api/agents` | Create a new custom agent |
| DELETE | `/api/agents/{agent_id}` | Disable an agent (set enabled=false in override) |
| POST | `/api/agents/reset` | Delete all user overrides (reset to defaults) |

**GET /api/agents** response:
```json
[
    {
        "id": "supervisor",
        "name": "Supervisor",
        "type": "supervisor",
        "system_prompt": "...",
        "model": "claude-sonnet-4-20250514",
        "tools": [],
        "vectorstore_ids": [],
        "enabled": true,
        "is_default": true
    },
    {
        "id": "data_analyst",
        "name": "Data Analyst",
        "type": "worker",
        "system_prompt": "...",
        "model": "claude-sonnet-4-20250514",
        "tools": ["mcp:sheerwater"],
        "vectorstore_ids": [],
        "enabled": true,
        "is_default": true
    }
]
```

The `is_default` field indicates whether this agent exists in the defaults (useful for the UI to know if "delete" means "remove custom agent" vs "disable default agent").

**PUT /api/agents/{agent_id}** request body:
```json
{
    "name": "Data Analyst",
    "system_prompt": "...",
    "model": "claude-sonnet-4-20250514",
    "tools": ["mcp:sheerwater"],
    "enabled": true
}
```

Handler:
1. Validate the input (construct an `AgentConfig` to validate)
2. Save to `user_agent_configs` table
3. Invalidate the graph cache for this user (clear the cache entry matching their current config hash, or just clear all -- the cache is small)
4. Return the updated effective config list

**POST /api/agents** request body:
```json
{
    "id": "my_custom_agent",
    "name": "My Custom Agent",
    "system_prompt": "...",
    "model": "claude-sonnet-4-20250514",
    "tools": []
}
```

Handler:
1. Validate: `id` must be unique (not in defaults, not already overridden for this user)
2. `type` is always set to "worker" (users cannot create supervisors)
3. Save as a user override
4. Invalidate graph cache
5. Return updated effective config list

**DELETE /api/agents/{agent_id}** handler:
1. If the agent is a default, save an override with `enabled=false`
2. If the agent is a custom user agent, delete the override row entirely
3. If the agent is the supervisor, return 400 error
4. Invalidate graph cache
5. Return updated effective config list

**POST /api/agents/reset** handler:
1. Delete all rows in `user_agent_configs` for this user
2. Invalidate graph cache (clear all for simplicity)
3. Return the default config list

**Modify POST /api/chat:**
- Change graph retrieval to: `graph = await get_agent_graph(user_id, db, mcp_tools, checkpointer)`

### `templates/config_editor.html` -- Config Editor Page

A full-page layout (not a modal) with navigation back to chat.

Structure:
```
+------------------------------------------+
| <- Back to Chat          Rhiza Agents     |
+------------------------------------------+
| Agent List        | Agent Details         |
|                   |                       |
| [Supervisor]      | Name: ____________    |
| [Data Analyst] *  | Model: [dropdown]     |
| [Code Runner]     | System Prompt:        |
| [Research Asst]   | [__________________]  |
|                   | [__________________]  |
| [+ Add Agent]     | Tools:                |
|                   | [x] mcp:sheerwater    |
|                   | [ ] sandbox:daytona   |
|                   |                       |
|                   | [Disable] [Save]      |
+-------------------+-----------------------+
|  [Reset All to Defaults]                  |
+------------------------------------------+
```

Features:
- Left panel: list of agents with active indicator. Click to select for editing.
- Right panel: form fields for the selected agent's config.
- System prompt: multi-line textarea
- Model: dropdown with Claude model options (claude-sonnet-4-20250514, claude-opus-4-20250514, claude-haiku-3-20240307)
- Tools: checkboxes for available tool types (in this phase, only "mcp:sheerwater" is a real option; "sandbox:daytona" shown but greyed out with "Coming soon")
- Enable/Disable toggle button (not shown for supervisor)
- Save button
- Add Agent button: opens a small form to create a new agent (id, name, system prompt)
- Reset All button: confirms then calls POST /api/agents/reset
- Disabled agents shown with strikethrough or dimmed in the list

### `static/config.js` -- Config Editor JavaScript

Functions:
- `loadAgents()` -- fetch GET /api/agents, populate agent list and detail panel
- `selectAgent(agentId)` -- show that agent's config in the detail panel
- `saveAgent()` -- PUT /api/agents/{id} with form data, then reload
- `createAgent()` -- POST /api/agents with new agent form data
- `deleteAgent(agentId)` -- DELETE /api/agents/{id}, then reload
- `resetAll()` -- confirm dialog, then POST /api/agents/reset, then reload
- Form validation: id must be alphanumeric with underscores, name required, system prompt required

### Modifications to `templates/chat.html`

Add a config/settings link in the sidebar footer:
```html
<a href="/config" class="config-link" title="Agent Config">Config</a>
```

### Modifications to `static/style.css`

Add styles for the config editor:
- Two-column layout (agent list + detail panel)
- Form field styles (reuse existing form-group styles from the modal)
- Agent list item styles (active state, disabled state with dimmed text)
- Responsive behavior for smaller screens
- "Coming soon" badge style for unavailable tools

## Reference Files

| File | What to learn |
|------|---------------|
| `/Users/tristan/Devel/rhiza/rhiza-agents/docs/ARCHITECTURE.md` | Config change flow, data model |
| `/Users/tristan/Devel/rhiza/rhiza-agents/src/rhiza_agents/db/sqlite.py` | Existing database patterns to extend |
| `/Users/tristan/Devel/rhiza/rhiza-agents/src/rhiza_agents/agents/registry.py` | Default configs to read |
| `/Users/tristan/Devel/rhiza/rhiza-agents/src/rhiza_agents/agents/graph.py` | Graph caching to integrate with |
| `/Users/tristan/Devel/rhiza/rhiza-agents/src/rhiza_agents/main.py` | Routes to add |
| `/Users/tristan/Devel/rhiza/rhiza-agents/src/rhiza_agents/static/style.css` | Existing CSS theme to extend |

## Acceptance Criteria

1. Navigate to `/config` -- see all four default agents listed
2. Select "Data Analyst" -- see its system prompt, model, and tools
3. Edit the data_analyst system prompt to say "Always respond in French", save
4. Go back to chat, ask "list available forecast models" -- get a French response from data_analyst
5. Go to `/config`, click "Reset All to Defaults", confirm
6. Go back to chat, ask the same question -- get an English response
7. Create a custom agent (e.g., "Haiku Writer" with a creative system prompt)
8. In chat, ask "write me a haiku" -- supervisor routes to the new agent
9. Disable "Code Runner" in config
10. The supervisor no longer mentions code_runner as a routing option (it's removed from the graph)
11. Re-enable "Code Runner" -- it reappears in the graph
12. The supervisor agent cannot be disabled or deleted (UI prevents it, API returns error)

## What NOT to Do

- **No vector store management in config editor** -- the `vectorstore_ids` field exists on the model but there's no UI to manage vector stores yet. That comes in Phase 5.
- **No document upload** -- Phase 5.
- **No sandbox tool** -- the "sandbox:daytona" checkbox appears in the UI but is greyed out. Phase 4 implements the actual tool.
- **No streaming** -- Phase 6.
- **No multi-user config isolation testing** -- configs are per-user via `user_id`, but don't add complex access control. Each user sees only their own overrides.
- **Do not modify the graph caching to be per-user keyed** -- the cache is keyed by config hash, which naturally handles per-user uniqueness (different config = different hash). If two users have the same effective config, they share the cached graph, which is fine.
