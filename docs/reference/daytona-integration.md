# Daytona Integration Reference

This document covers how rhiza-agents uses the Daytona SDK to provide sandboxed code execution for agents.

## Package Versions

| Package | Version | PyPI Name |
|---------|---------|-----------|
| `daytona-sdk` | 0.149.0 | `daytona_sdk` |

---

## Core API

### Initialization

```python
from daytona_sdk import Daytona, DaytonaConfig, CreateSandboxBaseParams

daytona = Daytona(DaytonaConfig(api_key="..."))
```

The API key comes from the `DAYTONA_API_KEY` environment variable.

### Creating a Sandbox

```python
sandbox = daytona.create(CreateSandboxBaseParams(language="python"))
```

`CreateSandboxBaseParams` parameters:

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `language` | `str` | Yes | Runtime language. Use `"python"` for Python sandboxes. |

### Running Code

```python
response = sandbox.process.code_run('print("hello")')
```

The `response` object has:

| Field | Type | Description |
|-------|------|-------------|
| `exit_code` | `int` | Process exit code. `0` = success. |
| `result` | `str` | Combined stdout/stderr output. |

### Deleting a Sandbox

```python
daytona.delete(sandbox)
```

Always delete sandboxes when done to free resources.

---

## File Operations

Sandboxes support file upload and download through `sandbox.fs`:

```python
# Upload a file to the sandbox
sandbox.fs.upload_file(destination_path="/home/user/data.csv", content=file_bytes)

# Download a file from the sandbox
file_content = sandbox.fs.download_file(path="/home/user/output.csv")
```

---

## Session Management Strategy

### One Sandbox Per Conversation

Each conversation (identified by `thread_id`) gets its own sandbox. This allows:
- Users to build on previous code executions within a conversation
- Installed packages to persist within a conversation
- File state to accumulate across multiple code runs

### Keying by thread_id

```python
# Store active sandboxes in a dict keyed by thread_id
active_sandboxes: dict[str, Sandbox] = {}

def get_or_create_sandbox(thread_id: str) -> Sandbox:
    if thread_id not in active_sandboxes:
        sandbox = daytona.create(CreateSandboxBaseParams(language="python"))
        active_sandboxes[thread_id] = sandbox
    return active_sandboxes[thread_id]
```

### Idle Timeout

Sandboxes should be cleaned up after **15 minutes** of idle time to avoid resource waste. Track last-used timestamps:

```python
from datetime import datetime, UTC

sandbox_last_used: dict[str, datetime] = {}

IDLE_TIMEOUT_MINUTES = 15

def cleanup_idle_sandboxes():
    now = datetime.now(UTC)
    expired = [
        tid for tid, last_used in sandbox_last_used.items()
        if (now - last_used).total_seconds() > IDLE_TIMEOUT_MINUTES * 60
    ]
    for tid in expired:
        if tid in active_sandboxes:
            daytona.delete(active_sandboxes.pop(tid))
            sandbox_last_used.pop(tid, None)
```

---

## Wrapping as a LangChain Tool

The code execution sandbox should be exposed as a LangChain tool that agents can call.

### Using @tool Decorator

```python
from langchain_core.tools import tool

@tool
def run_python_code(code: str) -> str:
    """Execute Python code in a sandboxed environment and return the output.

    Use this tool to run data analysis, computations, or any Python code.
    The sandbox persists across calls within the same conversation, so you
    can build on previous results, installed packages, and created files.

    Args:
        code: Python code to execute.

    Returns:
        The stdout/stderr output of the code execution, or an error message.
    """
    # thread_id must be injected from the graph config
    sandbox = get_or_create_sandbox(current_thread_id)
    response = sandbox.process.code_run(code)

    if response.exit_code != 0:
        return f"Error (exit code {response.exit_code}):\n{response.result}"
    return response.result
```

### Using BaseTool Subclass

For more control (e.g., accessing graph config to get `thread_id`):

```python
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field


class CodeExecutionInput(BaseModel):
    code: str = Field(description="Python code to execute")


class CodeExecutionTool(BaseTool):
    name: str = "run_python_code"
    description: str = (
        "Execute Python code in a sandboxed environment. "
        "The sandbox persists within the conversation."
    )
    args_schema: type = CodeExecutionInput

    # Instance state
    daytona_client: Daytona
    active_sandboxes: dict  # thread_id -> Sandbox

    def _run(self, code: str, config: dict | None = None) -> str:
        thread_id = config.get("configurable", {}).get("thread_id", "default")

        if thread_id not in self.active_sandboxes:
            sandbox = self.daytona_client.create(
                CreateSandboxBaseParams(language="python")
            )
            self.active_sandboxes[thread_id] = sandbox

        sandbox = self.active_sandboxes[thread_id]
        response = sandbox.process.code_run(code)

        if response.exit_code != 0:
            return f"Error (exit code {response.exit_code}):\n{response.result}"
        return response.result
```

---

## Environment Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `DAYTONA_API_KEY` | Yes | API key for Daytona service |
| `DAYTONA_API_URL` | No | Daytona API base URL (only needed for non-default endpoints) |
| `DAYTONA_PROXY_URL` | No | Override for sandbox proxy URL — required in Docker (see below) |

```python
import os
from daytona_sdk import Daytona, DaytonaConfig

daytona = Daytona(DaytonaConfig(
    api_key=os.environ["DAYTONA_API_KEY"],
    api_url=os.environ.get("DAYTONA_API_URL"),
))
```

---

## Proxy URL Fix (Critical for Docker)

The Daytona API returns a `toolboxProxyUrl` in sandbox creation responses (e.g., `http://proxy.localhost:4000/toolbox`). This URL is unreachable from Docker containers because `proxy.localhost` doesn't resolve to anything useful.

**Root cause:** The proxy URL is baked into the Daytona database during initial setup. The `PROXY_DOMAIN` env var on the Daytona API does NOT update it for existing deployments. Restarting the API, recreating containers, updating the DB region table, and restarting Redis all fail to change it.

**Fix:** Override the URL in the SDK after sandbox creation:

```python
def _patch_proxy_url(sandbox):
    proxy_url = os.environ.get("DAYTONA_PROXY_URL")
    if proxy_url and hasattr(sandbox, "_toolbox_api"):
        sandbox._toolbox_api._toolbox_base_url = proxy_url
```

Call this immediately after `daytona.create()`. Set `DAYTONA_PROXY_URL` to the reachable address (e.g., `http://<host-ip>:4000/toolbox`).

**Symptoms if not patched:** `ConnectionError` or timeouts when the agent tries to execute code. Logs will show the SDK trying to connect to `proxy.localhost:4000`.

---

## Integration with LangGraph Agents

The code execution tool is passed to agents like any other tool:

```python
from langgraph.prebuilt import create_react_agent
from langchain_anthropic import ChatAnthropic

model = ChatAnthropic(model="claude-sonnet-4-20250514")

code_tool = CodeExecutionTool(
    daytona_client=daytona,
    active_sandboxes={},
)

code_agent = create_react_agent(
    model=model,
    tools=[code_tool],
    name="code_agent",
    prompt="You are a data analysis agent. Use the run_python_code tool to execute Python code for analysis.",
)
```

---

## LangChain Deep Agents Reference

LangChain Deep Agents has an official Daytona integration that follows a similar pattern. The rhiza-agents integration adapts this for plain LangGraph rather than using the Deep Agents framework directly.

Key differences from the Deep Agents pattern:
- We use `create_react_agent` and `create_supervisor` instead of Deep Agents' orchestration
- Sandbox lifecycle is managed by the application, not by a framework
- The tool interface is a simple `BaseTool` subclass rather than a specialized executor

---

## Future: Self-Hosted Daytona

Currently using Daytona's hosted service. When Daytona's Kubernetes deployment matures, the plan is to switch to self-hosted:
- Deploy Daytona server in GKE
- Point `DaytonaConfig` at the in-cluster service URL instead of the hosted API
- Remove dependency on external API key (use in-cluster auth)

---

## Complete Example

```python
import os

from daytona_sdk import Daytona, DaytonaConfig, CreateSandboxBaseParams
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.prebuilt import create_react_agent

# Initialize Daytona
daytona = Daytona(DaytonaConfig(api_key=os.environ["DAYTONA_API_KEY"]))
sandboxes: dict[str, object] = {}


@tool
def run_python_code(code: str) -> str:
    """Execute Python code in a sandboxed environment.

    Args:
        code: Python code to execute.
    """
    # In practice, thread_id comes from the graph's RunnableConfig
    thread_id = "default"
    if thread_id not in sandboxes:
        sandboxes[thread_id] = daytona.create(
            CreateSandboxBaseParams(language="python")
        )
    sandbox = sandboxes[thread_id]
    response = sandbox.process.code_run(code)
    if response.exit_code != 0:
        return f"Error (exit code {response.exit_code}):\n{response.result}"
    return response.result


# Build agent
model = ChatAnthropic(model="claude-sonnet-4-20250514")
agent = create_react_agent(
    model=model,
    tools=[run_python_code],
    name="code_agent",
    prompt="You execute Python code to help with data analysis.",
)

# Invoke
checkpointer = SqliteSaver.from_conn_string("/data/checkpoints.db")
result = agent.invoke(
    {"messages": [HumanMessage(content="Calculate the first 10 Fibonacci numbers")]},
    config={"configurable": {"thread_id": "demo"}},
)
```
