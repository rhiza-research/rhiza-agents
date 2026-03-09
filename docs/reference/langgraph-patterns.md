# LangGraph Patterns Reference

This document covers the LangGraph API patterns used in rhiza-agents. All version numbers and API signatures are pinned to the versions used in this project.

## Package Versions

| Package | Version |
|---------|---------|
| `langgraph` | 1.0.10 |
| `langgraph-supervisor` | 0.0.31 |
| `langchain-anthropic` | 1.3.4 |
| `langgraph-checkpoint-sqlite` | 3.0.3 |

---

## Core Concepts: StateGraph

LangGraph models agent workflows as directed graphs. The fundamental building blocks are:

- **State**: A typed dictionary (usually `TypedDict` or `MessagesState`) that flows through the graph. Each node receives state and returns updates to it.
- **Nodes**: Python functions (sync or async) that receive state and return partial state updates.
- **Edges**: Connections between nodes. Can be unconditional (always follow) or conditional (route based on state).
- **Compilation**: Calling `.compile()` on a `StateGraph` produces a `CompiledGraph` that can be invoked.

### StateGraph Basics

```python
from langgraph.graph import StateGraph, MessagesState

# MessagesState is the standard state schema. It contains:
#   messages: list[BaseMessage]
# where BaseMessage is from langchain_core.messages.
# Messages are automatically merged (not replaced) when a node returns
# {"messages": [new_message]}.

graph_builder = StateGraph(MessagesState)

# Add nodes (functions that take state and return partial state updates)
def my_node(state: MessagesState) -> dict:
    # Process state["messages"]
    return {"messages": [AIMessage(content="response")]}

graph_builder.add_node("my_node", my_node)

# Add edges
graph_builder.add_edge("__start__", "my_node")
graph_builder.add_edge("my_node", "__end__")

# Compile
graph = graph_builder.compile()
```

### MessagesState

`MessagesState` is imported from `langgraph.graph`:

```python
from langgraph.graph import MessagesState
```

It is a `TypedDict` with a single key:

```python
class MessagesState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
```

The `add_messages` annotation means that when a node returns `{"messages": [...]}`, the new messages are **appended** to the existing list (not replaced). This is how conversation history accumulates.

---

## create_react_agent (Prebuilt)

`create_react_agent` creates a tool-calling agent from a model and a list of tools. It returns a **compiled graph** (not a StateGraph).

```python
from langgraph.prebuilt import create_react_agent
from langchain_anthropic import ChatAnthropic
from langchain_core.tools import tool

model = ChatAnthropic(model="claude-sonnet-4-20250514")

@tool
def my_tool(query: str) -> str:
    """Tool description shown to the LLM."""
    return "result"

agent = create_react_agent(
    model=model,           # Required. The LLM to use.
    tools=[my_tool],       # Required. List of LangChain BaseTool objects.
    name="my_agent",       # Optional. Name used for identification in multi-agent setups.
    prompt="You are a helpful assistant.",  # Optional. System prompt (state_modifier).
)
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `model` | `BaseChatModel` | Yes | The LLM (e.g., `ChatAnthropic`) |
| `tools` | `list[BaseTool]` | Yes | LangChain tool objects the agent can call |
| `name` | `str` | No | Agent name, used as identifier in supervisor setups |
| `prompt` | `str` | No | System prompt prepended to messages (acts as `state_modifier`) |

### Return Value

Returns a `CompiledGraph`. This is already compiled -- do **not** call `.compile()` on it again.

### Usage

```python
result = agent.invoke({"messages": [HumanMessage(content="What's the weather?")]})
# result["messages"] contains the full conversation including tool calls and responses
```

---

## create_supervisor (langgraph-supervisor 0.0.31)

`create_supervisor` creates a supervisor agent that orchestrates multiple sub-agents. It returns a **StateGraph** (not compiled).

```python
from langgraph_supervisor import create_supervisor
from langchain_anthropic import ChatAnthropic

supervisor_model = ChatAnthropic(model="claude-sonnet-4-20250514")

# Each agent must be a compiled graph (e.g., from create_react_agent)
weather_agent = create_react_agent(model=model, tools=[weather_tool], name="weather_agent")
data_agent = create_react_agent(model=model, tools=[data_tool], name="data_agent")

supervisor_graph = create_supervisor(
    agents=[weather_agent, data_agent],  # Required. List of compiled agent graphs.
    model=supervisor_model,              # Required. LLM for the supervisor.
    prompt="You route requests to the appropriate agent.",  # Optional. Supervisor system prompt.
    output_mode="full_history",          # Optional. "full_history" preserves all messages.
    add_handoff_back_messages=True,       # Optional. Adds "back to supervisor" messages.
)
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agents` | `list[CompiledGraph]` | Yes | Sub-agent compiled graphs (from `create_react_agent`) |
| `model` | `BaseChatModel` | Yes | LLM for the supervisor's routing decisions |
| `prompt` | `str` | No | System prompt for the supervisor |
| `output_mode` | `str` | No | `"full_history"` to preserve all messages from sub-agents |
| `add_handoff_back_messages` | `bool` | No | If `True`, adds messages when control returns to supervisor |

### Return Value

Returns a **`StateGraph`** (not compiled). You must call `.compile()` yourself:

```python
from langgraph.checkpoint.sqlite import SqliteSaver

checkpointer = SqliteSaver.from_conn_string("path/to/checkpoints.db")
compiled_supervisor = supervisor_graph.compile(checkpointer=checkpointer)
```

### Important: compile() is where you attach the checkpointer

The checkpointer is passed to `.compile()`, not to `create_supervisor()`. This is a key difference from `create_react_agent` which returns an already-compiled graph.

---

## ChatAnthropic (langchain-anthropic 1.3.4)

```python
from langchain_anthropic import ChatAnthropic

model = ChatAnthropic(
    model="claude-sonnet-4-20250514",  # Required. Model identifier.
    api_key="sk-...",                  # Optional if ANTHROPIC_API_KEY env var is set.
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

## SqliteSaver (langgraph-checkpoint-sqlite 3.0.3)

The checkpointer persists graph state (including all messages) across invocations. Use the **sync** version.

```python
from langgraph.checkpoint.sqlite import SqliteSaver

# Create from file path (sync version)
checkpointer = SqliteSaver.from_conn_string("path/to/checkpoints.db")

# Pass to compile()
compiled_graph = supervisor_graph.compile(checkpointer=checkpointer)
```

### Key Points

- Use `SqliteSaver.from_conn_string("path/to/db")` -- takes a plain file path, not a `sqlite:///` URL.
- Use the **sync** version (`SqliteSaver`), not the async version (`AsyncSqliteSaver`).
- The checkpointer manages its own schema -- do not create tables manually.
- For future Postgres migration: swap to `PostgresSaver` from `langgraph.checkpoint.postgres` (package: `langgraph-checkpoint-postgres`).

---

## Graph Invocation

### Basic Invocation

```python
from langchain_core.messages import HumanMessage

result = compiled_graph.invoke(
    {"messages": [HumanMessage(content="What is the forecast for Kenya?")]},
    config={"configurable": {"thread_id": "conversation-uuid-here"}}
)

# result["messages"] is the full message list including the new response
assistant_response = result["messages"][-1].content
```

### The config Parameter

The `config` dict is required for checkpointed graphs. The key field is:

```python
config = {
    "configurable": {
        "thread_id": "unique-conversation-id"
    }
}
```

### How thread_id Works with Checkpointing

- **Same thread_id = same conversation**. Messages persist across invocations.
- When you invoke a graph with a thread_id that has prior state, the checkpointer loads all previous messages before processing the new input.
- You do **not** need to pass previous messages manually -- the checkpointer handles this.
- A new thread_id starts a fresh conversation with no history.

Example of multi-turn conversation:

```python
thread_id = "abc-123"
config = {"configurable": {"thread_id": thread_id}}

# First message
result1 = graph.invoke(
    {"messages": [HumanMessage(content="What's the weather in Nairobi?")]},
    config=config
)

# Second message -- previous messages are loaded automatically by checkpointer
result2 = graph.invoke(
    {"messages": [HumanMessage(content="How about Mombasa?")]},
    config=config
)
# result2["messages"] contains ALL messages from both turns
```

### Extracting the Response

After invocation, the full message history is in `result["messages"]`. The last message is typically the final assistant response:

```python
from langchain_core.messages import AIMessage

last_message = result["messages"][-1]
if isinstance(last_message, AIMessage):
    response_text = last_message.content
```

---

## Caching Compiled Graphs

Compiled graphs are cheap to create but can be cached if you want to avoid recompilation:

```python
from functools import lru_cache

@lru_cache(maxsize=None)
def get_compiled_graph(config_hash: str):
    """Cache compiled graph per configuration."""
    # Build and compile graph based on config
    supervisor = create_supervisor(agents=[...], model=model, ...)
    return supervisor.compile(checkpointer=checkpointer)
```

The config_hash should capture anything that affects graph structure (e.g., which agents are enabled, which tools are available). Thread-level config (like thread_id) is passed at invocation time, not compilation time.

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

## Error Handling

LangGraph does not automatically retry on LLM errors. Wrap invocations if you need retry logic:

```python
from langchain_core.runnables import RunnableConfig

try:
    result = graph.invoke(
        {"messages": [HumanMessage(content="...")]},
        config={"configurable": {"thread_id": thread_id}}
    )
except Exception as e:
    # Handle LLM errors, tool errors, etc.
    pass
```

---

## Complete Example: Supervisor with Checkpointing

```python
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.prebuilt import create_react_agent
from langgraph_supervisor import create_supervisor

# 1. Create the model
model = ChatAnthropic(model="claude-sonnet-4-20250514")

# 2. Define tools
@tool
def get_forecast(region: str) -> str:
    """Get weather forecast for a region."""
    return f"Sunny in {region}"

# 3. Create sub-agents (returns CompiledGraph)
weather_agent = create_react_agent(
    model=model,
    tools=[get_forecast],
    name="weather_agent",
    prompt="You provide weather forecasts.",
)

# 4. Create supervisor (returns StateGraph, NOT compiled)
supervisor = create_supervisor(
    agents=[weather_agent],
    model=model,
    prompt="Route weather questions to the weather agent.",
    output_mode="full_history",
    add_handoff_back_messages=True,
)

# 5. Compile with checkpointer
checkpointer = SqliteSaver.from_conn_string("/data/checkpoints.db")
graph = supervisor.compile(checkpointer=checkpointer)

# 6. Invoke
result = graph.invoke(
    {"messages": [HumanMessage(content="What's the forecast for Kenya?")]},
    config={"configurable": {"thread_id": "conv-001"}}
)
```
