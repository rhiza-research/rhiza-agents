# MCP Integration Reference

This document covers how rhiza-agents connects to MCP (Model Context Protocol) servers and exposes their tools to LangGraph agents.

## Package Versions

| Package | Version |
|---------|---------|
| `langchain-mcp-adapters` | 0.2.1 |

---

## MultiServerMCPClient

`MultiServerMCPClient` from `langchain_mcp_adapters` is the bridge between MCP servers and LangChain/LangGraph. It connects to one or more MCP servers and converts their tools into LangChain `BaseTool` objects.

```python
from langchain_mcp_adapters.client import MultiServerMCPClient
```

### Connection Configuration

The client accepts a dictionary mapping server names to connection configs:

```python
mcp_config = {
    "sheerwater": {
        "url": "http://sheerwater-mcp:8000/sse",
        "transport": "sse",
    }
}

async with MultiServerMCPClient(mcp_config) as client:
    tools = client.get_tools()
    # tools is a list of LangChain BaseTool objects
```

### Connection Config Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | `str` | Yes | Full URL to the MCP server's SSE endpoint |
| `transport` | `str` | Yes | Transport type. Use `"sse"` for HTTP SSE transport. |

### get_tools()

`client.get_tools()` returns a `list[BaseTool]`. Each tool:
- Has a `.name` matching the MCP tool name (e.g., `tool_list_forecasts`)
- Has a `.description` from the MCP tool's description
- Has an auto-generated Pydantic input schema from the MCP tool's `inputSchema`
- Can be passed directly to `create_react_agent(tools=[...])`

### Stateless Behavior

`MultiServerMCPClient` is **stateless by default** -- each tool invocation creates a fresh MCP session. This means:
- No session state persists between tool calls
- No need to manage MCP session lifecycle per conversation
- The client handles connection pooling internally

---

## Sheerwater MCP Server

### Endpoints by Environment

| Environment | URL |
|-------------|-----|
| GKE (in-cluster) | `http://sheerwater-mcp.sheerwater-mcp.svc.cluster.local:8000/sse` |
| docker-compose | `http://sheerwater-mcp:8000/sse` |

### Available Tools

The sheerwater MCP server exposes the following tools:

| Tool Name | Description |
|-----------|-------------|
| `tool_list_forecasts` | List available forecast models for benchmarking |
| `tool_list_metrics` | List available evaluation metrics |
| `tool_list_truth_datasets` | List available ground truth datasets |
| `tool_get_metric_info` | Get detailed explanation of a specific metric |
| `tool_run_metric` | Run a single evaluation metric comparing forecast to truth |
| `tool_compare_models` | Compare multiple forecast models on a metric |
| `tool_estimate_query_time` | Estimate how long a query will take |
| `tool_extract_truth_data` | Extract ground truth data for a region/time period |
| `tool_render_plotly` | Render a Plotly chart specification to an image |
| `tool_get_dashboard_link` | Get a Grafana dashboard URL for exploration |
| `tool_generate_comparison_chart` | Generate a chart comparing models |

---

## Tool Filtering

When building agents, you may want to give different agents access to different subsets of MCP tools. Use a tool ID scheme to specify which tools an agent gets:

| Pattern | Meaning |
|---------|---------|
| `mcp:sheerwater` | All tools from the sheerwater MCP server |
| `mcp:sheerwater:tool_run_metric` | Only the `tool_run_metric` tool |
| `mcp:sheerwater:tool_list_forecasts` | Only the `tool_list_forecasts` tool |

### Filtering Implementation

After loading tools, filter them by name:

```python
all_tools = client.get_tools()

# Get specific tools by name
wanted = {"tool_run_metric", "tool_compare_models", "tool_list_forecasts"}
filtered_tools = [t for t in all_tools if t.name in wanted]
```

---

## Tool Loading and Caching

Tools should be loaded **once at app startup** and cached for the lifetime of the process. Do not re-fetch tools on every request.

### Recommended Pattern

```python
from contextlib import asynccontextmanager
from langchain_mcp_adapters.client import MultiServerMCPClient

# Global state
mcp_client: MultiServerMCPClient | None = None
mcp_tools: list = []

@asynccontextmanager
async def lifespan(app):
    global mcp_client, mcp_tools

    mcp_config = {
        "sheerwater": {
            "url": config.mcp_server_url,
            "transport": "sse",
        }
    }

    async with MultiServerMCPClient(mcp_config) as client:
        mcp_client = client
        mcp_tools = client.get_tools()
        yield

    mcp_client = None
    mcp_tools = []
```

Then when creating agents:

```python
agent = create_react_agent(
    model=model,
    tools=mcp_tools,  # Use cached tools
    name="sheerwater_agent",
    prompt="You analyze weather forecast data.",
)
```

---

## MCP Server Instructions

MCP servers can provide guidance text (instructions) during initialization. This text describes how the server's tools should be used and should be appended to agent system prompts.

### Retrieving Instructions

With the raw MCP client (from the `mcp` package), instructions come from the `initialize()` response:

```python
from mcp import ClientSession
from mcp.client.sse import sse_client

async with sse_client(server_url) as (read_stream, write_stream):
    async with ClientSession(read_stream, write_stream) as session:
        init_result = await session.initialize()
        instructions = init_result.instructions  # str or None
```

With `MultiServerMCPClient`, instructions are not directly exposed. If you need server instructions, you may need to make a separate MCP client connection to retrieve them, or hardcode them in agent prompts.

### Using Instructions in Agent Prompts

If the MCP server provides instructions, append them to the agent's system prompt:

```python
base_prompt = "You are a weather analysis agent."
if mcp_instructions:
    full_prompt = f"{base_prompt}\n\n{mcp_instructions}"
else:
    full_prompt = base_prompt

agent = create_react_agent(
    model=model,
    tools=mcp_tools,
    name="weather_agent",
    prompt=full_prompt,
)
```

---

## SSE Transport Note

The current MCP integration uses SSE (Server-Sent Events) transport. SSE is **deprecated** in the MCP specification in favor of streamable-http transport. The codebase may need to migrate to streamable-http in the future.

When that migration happens:
- The `transport` field in the config will change from `"sse"` to `"streamable-http"`
- The URL will likely change from `/sse` to a different path
- The `langchain-mcp-adapters` package will need to support the new transport

---

## Complete Example: MCP Tools with LangGraph Agent

```python
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.prebuilt import create_react_agent
from langgraph_supervisor import create_supervisor

async def build_graph(mcp_server_url: str):
    """Build and return a compiled supervisor graph with MCP tools."""

    model = ChatAnthropic(model="claude-sonnet-4-20250514")

    mcp_config = {
        "sheerwater": {
            "url": mcp_server_url,
            "transport": "sse",
        }
    }

    async with MultiServerMCPClient(mcp_config) as client:
        tools = client.get_tools()

        # Create agent with MCP tools
        sheerwater_agent = create_react_agent(
            model=model,
            tools=tools,
            name="sheerwater_agent",
            prompt="You analyze weather forecast benchmarking data using the sheerwater tools.",
        )

        # Create supervisor
        supervisor = create_supervisor(
            agents=[sheerwater_agent],
            model=model,
            prompt="Route data analysis questions to the sheerwater agent.",
            output_mode="full_history",
            add_handoff_back_messages=True,
        )

        checkpointer = SqliteSaver.from_conn_string("/data/checkpoints.db")
        graph = supervisor.compile(checkpointer=checkpointer)

        # Use the graph while MCP client is alive
        result = graph.invoke(
            {"messages": [HumanMessage(content="Compare ECMWF and FuXi on MAE")]},
            config={"configurable": {"thread_id": "demo"}}
        )

        return result
```

**Important**: The `MultiServerMCPClient` context manager must remain open for the lifetime that tools are being called. If the context manager exits, tool invocations will fail. This is why tools are loaded during the app lifespan.
