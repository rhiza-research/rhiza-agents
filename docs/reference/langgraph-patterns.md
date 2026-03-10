# LangGraph & Deep Agents Patterns Reference

This document covers the LangGraph and Deep Agents API patterns used in rhiza-agents.

## Package Versions

| Package | Purpose |
|---------|---------|
| `deepagents` | `create_deep_agent()` — agent harness with planning, subagents, context management |
| `langgraph` | Graph framework underlying Deep Agents |
| `langchain-anthropic` | Claude model binding |

---

## create_deep_agent (deepagents)

`create_deep_agent` creates a deep agent with built-in planning, subagent spawning, todo list capabilities, and file operations. It returns a compiled LangGraph graph.

```python
from deepagents import create_deep_agent
from langchain_anthropic import ChatAnthropic

model = ChatAnthropic(model="claude-sonnet-4-20250514")

graph = create_deep_agent(
    model=model,
    tools=[...],           # LangChain BaseTool objects
    system_prompt="...",   # System prompt for the agent
    subagents={...},       # Optional subagent definitions
    middleware=[...],      # Optional middleware (retry, tool limits, summarization)
)
```

### Return Value

Returns a compiled LangGraph graph. This is what LangGraph Server imports and serves via `langgraph.json`.

### Key Features (built-in)

- **Planning**: The agent can break down complex requests into steps
- **Subagents**: Delegate tasks to specialized subagents for context isolation
- **Context management**: Built-in handling of conversation context
- **File operations**: File read/write capabilities out of the box

---

## ChatAnthropic (langchain-anthropic)

```python
from langchain_anthropic import ChatAnthropic

model = ChatAnthropic(
    model="claude-sonnet-4-20250514",  # Required. Model identifier.
    # api_key is read from ANTHROPIC_API_KEY env var automatically.
)
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `model` | `str` | Yes | Anthropic model ID (e.g., `"claude-sonnet-4-20250514"`) |
| `api_key` | `str` | No | API key. Falls back to `ANTHROPIC_API_KEY` env var. |
| `max_tokens` | `int` | No | Maximum tokens in response |
| `temperature` | `float` | No | Sampling temperature |

---

## LangGraph Server & Checkpointing

In the new architecture, LangGraph Server (self-hosted) handles:
- **Checkpointing**: PostgreSQL-backed persistence of graph state and message history
- **Thread management**: Thread creation, listing, deletion via REST API
- **Streaming**: SSE-based streaming of agent responses
- **Task queue**: Redis-backed async run execution

The agent code does **not** manage checkpointing directly. LangGraph Server handles this when it imports and serves the graph defined in `langgraph.json`.

### langgraph.json

```json
{
  "graphs": {
    "agent": "./src/rhiza_agents/agent.py:graph"
  }
}
```

This tells LangGraph Server where to find the compiled graph object.

---

## Common Message Types

```python
from langchain_core.messages import (
    HumanMessage,    # User input
    AIMessage,       # Model response
    SystemMessage,   # System prompt (rarely used directly with LangGraph)
    ToolMessage,     # Tool call result
)
```

---

## RunnableConfig for Tool Context

Tools can access graph configuration (e.g., `thread_id`) via `RunnableConfig` injection:

```python
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

@tool
async def my_tool(arg: str, *, config: RunnableConfig) -> str:
    """Tool that needs access to the thread context."""
    thread_id = config.get("configurable", {}).get("thread_id", "default")
    # ... use thread_id for per-conversation state
```

This pattern is used by the Daytona sandbox tool to maintain one sandbox per conversation.
