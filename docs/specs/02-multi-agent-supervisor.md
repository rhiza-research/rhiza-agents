# Phase 2: Multi-Agent with Supervisor

## Goal

Replace the single ReAct agent from Phase 1 with a supervisor + sub-agent architecture. The supervisor receives every user message and routes it to the appropriate specialized sub-agent. This phase introduces the `AgentConfig` model, a registry of default agent definitions, and dynamic graph construction.

## Prerequisites

Phase 1 must be complete and working:
- FastAPI app with Keycloak auth, conversation persistence, MCP tools
- `create_react_agent` with `AsyncSqliteSaver` checkpointer
- All files from Phase 1 exist and the app runs via `docker compose up`

## Files to Create

```
src/rhiza_agents/db/models.py
src/rhiza_agents/agents/registry.py
src/rhiza_agents/agents/graph.py
src/rhiza_agents/agents/supervisor.py
```

## Files to Modify

```
src/rhiza_agents/main.py
src/rhiza_agents/templates/chat.html
src/rhiza_agents/static/chat.js
src/rhiza_agents/static/style.css
```

## Key APIs & Packages

```python
# Supervisor creation
from langgraph_supervisor import create_supervisor

# Agent creation (same as Phase 1)
from langgraph.prebuilt import create_react_agent
from langchain_anthropic import ChatAnthropic

# Pydantic for config model
from pydantic import BaseModel, Field

# Checkpointer (same as Phase 1)
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
```

## Implementation Details

### `db/models.py` -- AgentConfig Pydantic Model

```python
from pydantic import BaseModel, Field

class AgentConfig(BaseModel):
    id: str                              # unique identifier, e.g. "data_analyst"
    name: str                            # display name, e.g. "Data Analyst"
    type: str                            # "supervisor" or "worker"
    system_prompt: str                   # the agent's system prompt
    model: str = "claude-sonnet-4-20250514"  # Claude model identifier
    tools: list[str] = Field(default_factory=list)    # tool identifiers, e.g. ["mcp:sheerwater"]
    vectorstore_ids: list[str] = Field(default_factory=list)  # for Phase 5
    enabled: bool = True                 # whether this agent is active in the graph
```

The `tools` field uses a namespaced identifier scheme:
- `mcp:sheerwater` -- all tools from the sheerwater MCP server
- `sandbox:daytona` -- Daytona sandbox tool (Phase 4)
- `vectordb:{collection_id}` -- vector store retrieval tool (Phase 5)

In this phase, only `mcp:sheerwater` is implemented. Other tool types are recognized but produce empty tool lists (the agent just responds conversationally without tools).

### `agents/registry.py` -- Default Agent Definitions

Contains a function `get_default_configs() -> list[AgentConfig]` that returns the hardcoded default agent configurations.

**Default agents:**

1. **Supervisor** (`supervisor`):
   - type: "supervisor"
   - system_prompt: Instructions for routing. Something like: "You are a routing supervisor. Analyze the user's message and delegate to the most appropriate agent. Use data_analyst for questions about weather forecasts, models, and metrics. Use code_runner for code execution tasks. Use research_assistant for questions about uploaded documents. For general conversation, respond directly."
   - tools: [] (supervisor only gets handoff tools, which are auto-generated)
   - model: "claude-sonnet-4-20250514"

2. **Data Analyst** (`data_analyst`):
   - type: "worker"
   - system_prompt: "You are a data analyst specializing in weather forecast models and benchmarking. Use your tools to fetch data, run evaluations, and create visualizations. Do not make up data -- always use tools to get real information. Be concise and format data clearly."
   - tools: ["mcp:sheerwater"]
   - model: "claude-sonnet-4-20250514"

3. **Code Runner** (`code_runner`):
   - type: "worker"
   - system_prompt: "You are a code execution assistant. You help users write and run Python code for data analysis, computation, and visualization. Write clean, well-commented code."
   - tools: [] (sandbox tool added in Phase 4)
   - model: "claude-sonnet-4-20250514"
   - enabled: true (responds conversationally without tools for now)

4. **Research Assistant** (`research_assistant`):
   - type: "worker"
   - system_prompt: "You are a research assistant. You answer questions using knowledge from uploaded documents and knowledge bases. Cite your sources when possible. If you don't have relevant documents, say so."
   - tools: [] (vector store tools added in Phase 5)
   - model: "claude-sonnet-4-20250514"
   - enabled: true (responds conversationally without tools for now)

Also provide a helper:

```python
def get_default_configs_by_id() -> dict[str, AgentConfig]:
    """Return default configs keyed by agent ID."""
    return {c.id: c for c in get_default_configs()}
```

### `agents/graph.py` -- Dynamic Graph Construction

This module builds a compiled LangGraph `StateGraph` from a list of `AgentConfig` objects.

**Key function:**

```python
async def build_graph(
    configs: list[AgentConfig],
    mcp_tools: list,
    checkpointer,
) -> CompiledGraph:
```

Logic:
1. Separate configs into supervisor config and worker configs (filter by `type` and `enabled`)
2. For each worker config:
   a. Resolve tools: if `"mcp:sheerwater"` in config.tools, use the `mcp_tools` list. Other tool types return empty lists for now.
   b. Create a `ChatAnthropic(model=config.model)` instance
   c. Create the worker agent: `create_react_agent(model, tools, prompt=config.system_prompt, name=config.id)`
3. Create the supervisor using `create_supervisor`:
   ```python
   supervisor = create_supervisor(
       model=ChatAnthropic(model=supervisor_config.model),
       agents=worker_agents,  # list of compiled worker graphs
       prompt=supervisor_config.system_prompt,
       output_mode="full_history",
       add_handoff_back_messages=True,
   )
   ```
4. Compile with checkpointer: `supervisor.compile(checkpointer=checkpointer)`
5. Return the compiled graph

**Graph caching:**

Maintain a module-level cache `_graph_cache: dict[str, CompiledGraph]` keyed by a hash of the config list. Compute the hash by serializing the list of `AgentConfig` objects to JSON and hashing.

```python
import hashlib
import json

def _config_hash(configs: list[AgentConfig]) -> str:
    data = json.dumps([c.model_dump() for c in configs], sort_keys=True)
    return hashlib.sha256(data.encode()).hexdigest()
```

Provide a function:

```python
async def get_or_build_graph(
    configs: list[AgentConfig],
    mcp_tools: list,
    checkpointer,
) -> CompiledGraph:
    h = _config_hash(configs)
    if h not in _graph_cache:
        _graph_cache[h] = await build_graph(configs, mcp_tools, checkpointer)
    return _graph_cache[h]
```

And a cache invalidation function:

```python
def invalidate_graph_cache(config_hash: str | None = None):
    """Invalidate cached graph. If config_hash is None, clear all."""
    if config_hash is None:
        _graph_cache.clear()
    else:
        _graph_cache.pop(config_hash, None)
```

### `agents/supervisor.py` -- Supervisor Convenience

This is a thin module that ties together registry + graph for the common case. It provides:

```python
async def get_agent_graph(
    mcp_tools: list,
    checkpointer,
    user_configs: list[AgentConfig] | None = None,
) -> CompiledGraph:
    """Get the compiled agent graph for a user.

    If user_configs is provided, use those. Otherwise use defaults.
    Config merging (user overrides on top of defaults) is handled by
    the caller -- this function just takes the final config list.
    """
    configs = user_configs or get_default_configs()
    return await get_or_build_graph(configs, mcp_tools, checkpointer)
```

This exists so `main.py` has a single function to call. In Phase 3, this function will accept user overrides.

### Modifications to `main.py`

**Lifespan changes:**
- Remove the single `create_react_agent` call
- Instead, after loading MCP tools and creating the checkpointer, just store them as globals
- The agent graph is built lazily on first chat request (or eagerly, either works)

**POST /api/chat changes:**
- Before invoking, get the graph: `graph = await get_agent_graph(mcp_tools, checkpointer)`
- Invoke: `result = await graph.ainvoke({"messages": [HumanMessage(content=message)]}, config={"configurable": {"thread_id": conversation_id}})`
- Extract response: the last message in `result["messages"]` should be the final AI response
- Extract which agent responded: check the `name` field on the AI message (LangGraph sets this to the node/agent name)

**Response format changes:**

Add `agent_name` to the response:
```json
{
    "conversation_id": "...",
    "response": "...",
    "tool_calls": [...],
    "agent_name": "data_analyst"
}
```

To determine which agent produced the final response, look at the messages in the result. The last `AIMessage` before the supervisor's final response will have a `name` attribute set to the worker agent's ID. Walk backward through `result["messages"]` to find the last AI message with actual content (not just a handoff).

**GET /c/{conversation_id} changes:**
- When extracting messages for display, include the `name` attribute from AIMessages so the UI can show which agent responded

### Modifications to `templates/chat.html`

- Add agent name badge to assistant messages: if a message has an `agent_name`, display it as a small label above or next to the message content
- Example: `<span class="agent-badge">Data Analyst</span>` before the message content

### Modifications to `static/chat.js`

- When rendering a new assistant message from the API response, include the `agent_name` as a badge
- When rendering server-side messages, look for the `agent_name` data attribute

### Modifications to `static/style.css`

Add styles for the agent badge:
```css
.agent-badge {
    display: inline-block;
    font-size: 0.75rem;
    color: #4a90d9;
    background: rgba(74, 144, 217, 0.15);
    padding: 0.15rem 0.5rem;
    border-radius: 3px;
    margin-bottom: 0.5rem;
    font-weight: 500;
}
```

## Reference Files

| File | What to learn |
|------|---------------|
| `/Users/tristan/Devel/rhiza/rhiza-agents/docs/ARCHITECTURE.md` | Agent topology, config model, data flow |
| `/Users/tristan/Devel/rhiza/rhiza-agents/src/rhiza_agents/main.py` | Current Phase 1 main.py to modify |
| `/Users/tristan/Devel/rhiza/rhiza-agents/src/rhiza_agents/agents/tools/mcp.py` | Current MCP tool loading |

For `langgraph-supervisor` API reference, use:
- `from langgraph_supervisor import create_supervisor` -- the `create_supervisor` function takes `model`, `agents` (list of compiled graphs), `prompt`, and options like `output_mode` and `add_handoff_back_messages`

For `create_react_agent`, the `name` parameter is what the supervisor uses for handoff tool names (`transfer_to_{name}`).

## Acceptance Criteria

1. App starts via `docker compose up` without errors
2. Ask "list available forecast models" -- supervisor routes to `data_analyst`, which calls MCP tools. Response shows "Data Analyst" badge.
3. Ask "write me a poem about weather" -- supervisor routes to a worker or handles directly. Response shows which agent responded.
4. Ask "what Python libraries are good for data analysis?" -- supervisor routes to `code_runner` (which responds conversationally since it has no tools yet). Response shows "Code Runner" badge.
5. Conversation history works correctly across the multi-agent graph (messages persist, reload shows full history with agent badges).
6. The tool list endpoint `/api/tools` still works.

## What NOT to Do

- **No user-editable config** -- agent configs are hardcoded defaults in `registry.py`. The config editor UI comes in Phase 3.
- **No database storage of agent configs** -- no `user_agent_configs` table yet. That's Phase 3.
- **No sandbox tools** -- code_runner has no tools, just responds conversationally. Phase 4.
- **No vector store tools** -- research_assistant has no tools. Phase 5.
- **No streaming** -- full response returned at once. Phase 6.
- **Do not modify the checkpointer setup** -- keep using `AsyncSqliteSaver` from Phase 1.
- **Do not change the auth flow** -- keep it exactly as Phase 1.
