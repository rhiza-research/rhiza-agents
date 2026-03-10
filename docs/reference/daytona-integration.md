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

The sandbox tool uses `RunnableConfig` injection to access the `thread_id` for per-conversation sandbox management:

```python
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

@tool
async def execute_python_code(code: str, *, config: RunnableConfig) -> str:
    """Execute Python code in a sandboxed environment and return the output.

    Use this tool to run data analysis, computations, or any Python code.
    The sandbox persists across calls within the same conversation.

    Args:
        code: Python code to execute.

    Returns:
        The stdout/stderr output of the code execution, or an error message.
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")
    sandbox = get_or_create_sandbox(thread_id)
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

```python
import os
from daytona_sdk import Daytona, DaytonaConfig

daytona = Daytona(DaytonaConfig(api_key=os.environ["DAYTONA_API_KEY"]))
```

---

## Integration with Deep Agents

The sandbox tool is given to a `code_runner` subagent in the deep agent configuration. This keeps code execution in a separate context from the main agent:

```python
from deepagents import create_deep_agent
from langchain_anthropic import ChatAnthropic

model = ChatAnthropic(model="claude-sonnet-4-20250514")

graph = create_deep_agent(
    model=model,
    tools=[execute_python_code],
    subagents={"code_runner": {
        "tools": [execute_python_code],
        "system_prompt": "You execute Python code for data analysis.",
    }},
    system_prompt="You analyze weather forecast data.",
)
```

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
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

# Initialize Daytona
daytona = Daytona(DaytonaConfig(api_key=os.environ["DAYTONA_API_KEY"]))
sandboxes: dict[str, object] = {}


@tool
async def execute_python_code(code: str, *, config: RunnableConfig) -> str:
    """Execute Python code in a sandboxed environment.

    Args:
        code: Python code to execute.
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")
    if thread_id not in sandboxes:
        sandboxes[thread_id] = daytona.create(
            CreateSandboxBaseParams(language="python")
        )
    sandbox = sandboxes[thread_id]
    response = sandbox.process.code_run(code)
    if response.exit_code != 0:
        return f"Error (exit code {response.exit_code}):\n{response.result}"
    return response.result
```
