# Phase 4: Sandboxed Code Execution

## Goal

The Code Runner agent can execute Python code in Daytona sandboxes. Users ask for code execution, the supervisor routes to code_runner, and code_runner writes Python code, executes it in a hosted sandbox, and returns the results (stdout, stderr, exit code).

## Prerequisites

Phase 3 must be complete and working:
- Supervisor + sub-agent architecture with dynamic graph building
- Config editor where users can toggle tools and edit agents
- `code_runner` agent exists in the registry but has no tools

## Files to Create

```
src/rhiza_agents/agents/tools/sandbox.py
```

## Files to Modify

```
src/rhiza_agents/config.py
src/rhiza_agents/agents/registry.py
src/rhiza_agents/agents/graph.py
src/rhiza_agents/templates/chat.html
src/rhiza_agents/static/chat.js
src/rhiza_agents/static/style.css
docker-compose.yml
```

## Key APIs & Packages

```python
# Daytona SDK
from daytona_sdk import Daytona, DaytonaConfig, CreateSandboxParams

# LangChain tool decorator
from langchain_core.tools import tool
```

The `daytona-sdk` package should already be in `pyproject.toml` dependencies (version 0.149.0 per ARCHITECTURE.md).

## Implementation Details

### Modifications to `config.py`

Add one new environment variable:

| Env Var | Required | Default | Purpose |
|---------|----------|---------|---------|
| `DAYTONA_API_KEY` | no | `""` | Daytona API key for sandbox creation. If empty, sandbox tool is unavailable. |

### `agents/tools/sandbox.py` -- Daytona Sandbox Tool

This module provides a LangChain tool that executes Python code in Daytona sandboxes.

**Sandbox lifecycle management:**

Maintain a module-level dictionary mapping thread_id (conversation UUID) to active sandbox instances:

```python
import asyncio
import time

_active_sandboxes: dict[str, dict] = {}
# Each entry: {"sandbox": sandbox_obj, "daytona": daytona_client, "last_used": float}

SANDBOX_IDLE_TIMEOUT = 900  # 15 minutes in seconds
```

**The tool function:**

```python
from langchain_core.tools import tool

def create_sandbox_tool(api_key: str):
    """Create a LangChain tool for code execution in Daytona sandboxes.

    Args:
        api_key: Daytona API key

    Returns:
        A LangChain tool function
    """

    @tool
    async def execute_python_code(code: str, thread_id: str = "") -> str:
        """Execute Python code in a sandboxed environment.

        Args:
            code: Python code to execute.

        Returns:
            Execution result with stdout, stderr, and exit code.
        """
        # Get or create sandbox for this thread
        sandbox_info = _active_sandboxes.get(thread_id)

        if sandbox_info is None:
            # Create new sandbox
            daytona = Daytona(DaytonaConfig(api_key=api_key))
            sandbox = daytona.create(CreateSandboxParams(language="python"))
            sandbox_info = {
                "sandbox": sandbox,
                "daytona": daytona,
                "last_used": time.time(),
            }
            _active_sandboxes[thread_id] = sandbox_info

        sandbox = sandbox_info["sandbox"]
        sandbox_info["last_used"] = time.time()

        # Execute code
        result = sandbox.process.code_run(code)

        # Format output
        output_parts = []
        if result.result:
            output_parts.append(f"Output:\n{result.result}")
        if result.output:
            output_parts.append(f"Stdout:\n{result.output}")
        if result.output_error:
            output_parts.append(f"Stderr:\n{result.output_error}")
        output_parts.append(f"Exit code: {result.exit_code}")

        return "\n\n".join(output_parts)

    return execute_python_code
```

**Important note about thread_id:** The tool needs the conversation's thread_id to maintain one sandbox per conversation. LangGraph passes the `config` to tools via `RunnableConfig`. The tool should access it through LangChain's injected config mechanism:

```python
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

@tool
async def execute_python_code(code: str, *, config: RunnableConfig) -> str:
    """Execute Python code in a sandboxed environment."""
    thread_id = config.get("configurable", {}).get("thread_id", "default")
    # ... rest of implementation
```

The `config` parameter with `RunnableConfig` type annotation is automatically injected by LangGraph and not exposed to the LLM as a tool parameter.

**Sandbox cleanup:**

Provide a background task function that cleans up idle sandboxes:

```python
async def cleanup_idle_sandboxes():
    """Remove sandboxes that have been idle for more than SANDBOX_IDLE_TIMEOUT seconds."""
    now = time.time()
    to_remove = []
    for thread_id, info in _active_sandboxes.items():
        if now - info["last_used"] > SANDBOX_IDLE_TIMEOUT:
            to_remove.append(thread_id)

    for thread_id in to_remove:
        info = _active_sandboxes.pop(thread_id, None)
        if info:
            try:
                info["daytona"].delete(info["sandbox"])
            except Exception:
                pass  # Best effort cleanup
```

Also provide a function to clean up a specific conversation's sandbox (called when a conversation is deleted):

```python
async def cleanup_sandbox(thread_id: str):
    """Clean up sandbox for a specific conversation."""
    info = _active_sandboxes.pop(thread_id, None)
    if info:
        try:
            info["daytona"].delete(info["sandbox"])
        except Exception:
            pass
```

### Modifications to `agents/registry.py`

Update the `code_runner` default config to include the sandbox tool:

```python
AgentConfig(
    id="code_runner",
    name="Code Runner",
    type="worker",
    system_prompt=(
        "You are a code execution assistant. You help users write and run Python code "
        "for data analysis, computation, and visualization. "
        "When asked to run code, write the Python code and execute it using the "
        "execute_python_code tool. Present the results clearly. "
        "If code fails, analyze the error and try a corrected version."
    ),
    model="claude-sonnet-4-20250514",
    tools=["sandbox:daytona"],
    enabled=True,
)
```

### Modifications to `agents/graph.py`

Update the existing `_resolve_tools` function in `graph.py` to handle `sandbox:daytona`. The function already exists as a standalone function (not a method):

```python
def _resolve_tools(config: AgentConfig, mcp_tools: list, daytona_api_key: str = "") -> list:
    """Resolve tool identifiers to actual tool objects."""
    tools = []
    for tool_id in config.tools:
        if tool_id == "mcp:sheerwater":
            tools.extend(mcp_tools)
        elif tool_id == "sandbox:daytona":
            if daytona_api_key:
                from .tools.sandbox import create_sandbox_tool
                tools.append(create_sandbox_tool(daytona_api_key))
            # If no API key, silently skip -- agent works without tools
        else:
            logger.info("Tool type %s not yet implemented, skipping", tool_id)
    return tools
```

Update `build_graph` and `get_or_build_graph` signatures to accept `daytona_api_key`:

```python
async def build_graph(
    configs: list[AgentConfig],
    mcp_tools: list,
    checkpointer,
    daytona_api_key: str = "",
) -> CompiledGraph:
```

### Modifications to `main.py`

1. Add `DAYTONA_API_KEY` to the config loaded at startup
2. Pass `daytona_api_key=config.daytona_api_key` through to `get_agent_graph` and then to `build_graph`. Note: after Phase 3, `get_agent_graph` signature is `get_agent_graph(mcp_tools, checkpointer, user_configs=None, user_id=None, db=None)` and it may need a `daytona_api_key` parameter added, which it passes through to `get_or_build_graph` and `build_graph`.
3. Start the sandbox cleanup background task in the lifespan:

```python
async def _sandbox_cleanup_loop():
    """Background task to clean up idle sandboxes."""
    while True:
        await asyncio.sleep(60)  # Check every minute
        await cleanup_idle_sandboxes()
```

In the lifespan:
```python
cleanup_task = asyncio.create_task(_sandbox_cleanup_loop())
yield
cleanup_task.cancel()
```

4. When a conversation is deleted (DELETE /api/conversations/{id}), also call `cleanup_sandbox(conversation_id)`.

### Modifications to `docker-compose.yml`

Add `DAYTONA_API_KEY` to the rhiza-agents service environment:
```yaml
DAYTONA_API_KEY: ${DAYTONA_API_KEY:-}
```

This reads from the host environment. If not set, sandbox tool is unavailable and code_runner responds conversationally.

### Modifications to `templates/chat.html`

Add rendering for code execution results in assistant messages. When the response contains tool calls with name `execute_python_code`, render the results in a distinct visual block:

```html
<!-- Code execution result block -->
<div class="code-execution">
    <div class="code-execution-header">Code Execution</div>
    <pre class="code-execution-output">{{ output }}</pre>
</div>
```

The tool call display should show the code that was executed (from tool_calls input) and the result. This is handled in the JavaScript when rendering messages.

### Modifications to `static/chat.js`

When rendering tool calls, check if the tool name is `execute_python_code`. If so:
- Show the `code` parameter from the tool input as a syntax-highlighted Python code block
- Show the execution result in a separate output block
- This makes it clear what code was run and what the output was

The tool call data available in the response includes:
```json
{
    "name": "execute_python_code",
    "input": {"code": "print('hello world')"},
    "output": "Output:\nhello world\n\nExit code: 0"
}
```

To get tool outputs in the API response, modify the message extraction logic in `main.py` to include tool results from `ToolMessage` objects in the conversation history. Add a `tool_results` field to the response (or embed them in the `tool_calls` list).

### Modifications to `static/style.css`

Add styles for code execution display:

```css
.code-execution {
    margin-top: 0.75rem;
    border: 1px solid #2a4a2a;
    border-radius: 6px;
    overflow: hidden;
}

.code-execution-header {
    background: #1a3a1a;
    padding: 0.5rem 0.75rem;
    font-size: 0.8rem;
    color: #6a9a6a;
    font-weight: 500;
}

.code-execution-output {
    background: #0d1117;
    padding: 0.75rem;
    margin: 0;
    font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
    font-size: 0.85rem;
    line-height: 1.5;
    color: #c9d1d9;
    white-space: pre-wrap;
    overflow-x: auto;
}
```

### Config Editor Update

In the config editor (from Phase 3), the "sandbox:daytona" tool checkbox should now be functional (not greyed out) when `DAYTONA_API_KEY` is configured. The API could expose available tool types at a new endpoint:

```
GET /api/tool-types
```

Response:
```json
[
    {"id": "mcp:sheerwater", "name": "Sheerwater MCP Tools", "available": true},
    {"id": "sandbox:daytona", "name": "Code Sandbox (Daytona)", "available": true}
]
```

The `available` field is `true` when the necessary API key / connection is configured. The config editor uses this to decide whether to grey out or enable the checkbox.

## Reference Files

| File | What to learn |
|------|---------------|
| `/Users/tristan/Devel/rhiza/rhiza-agents/docs/ARCHITECTURE.md` | Sandbox integration section, Daytona SDK usage |
| `/Users/tristan/Devel/rhiza/rhiza-agents/src/rhiza_agents/agents/graph.py` | Where to add tool resolution |
| `/Users/tristan/Devel/rhiza/rhiza-agents/src/rhiza_agents/agents/registry.py` | Where to update code_runner config |
| `/Users/tristan/Devel/rhiza/rhiza-agents/src/rhiza_agents/main.py` | Where to add background cleanup task |

For the Daytona SDK API, the key classes are:
- `Daytona(DaytonaConfig(api_key=...))` -- client constructor
- `daytona.create(CreateSandboxParams(language="python"))` -- create a sandbox
- `sandbox.process.code_run(code)` -- execute code, returns result with `.result`, `.output`, `.output_error`, `.exit_code`
- `daytona.delete(sandbox)` -- destroy sandbox

Check the installed `daytona-sdk` package for exact API signatures -- the SDK may have changed from version 0.149.0. Inspect the installed package if needed:
```python
import daytona_sdk
help(daytona_sdk.Daytona)
```

## Acceptance Criteria

1. Set `DAYTONA_API_KEY` in environment, restart the app
2. Ask "write Python code to calculate the first 20 fibonacci numbers and print them"
3. Supervisor routes to code_runner
4. Code_runner writes Python code and calls `execute_python_code`
5. See the code displayed in a syntax-highlighted block
6. See the execution output (the fibonacci numbers) in a separate output block
7. The response shows "Code Runner" agent badge
8. Ask a follow-up: "now make it a generator function" -- code_runner reuses the same sandbox
9. After 15 minutes of inactivity, the sandbox is cleaned up (verify in logs)
10. In the config editor, "sandbox:daytona" checkbox is now functional (not greyed out)
11. If `DAYTONA_API_KEY` is not set, code_runner responds conversationally without code execution

## What NOT to Do

- **No file upload to sandbox** -- only code execution via the tool. Users cannot upload files to the sandbox.
- **No persistent sandbox state across conversations** -- each conversation gets its own sandbox. Starting a new conversation starts a fresh sandbox.
- **No self-hosted Daytona** -- use the hosted Daytona service only. No Docker-in-Docker setup.
- **No pre-installed packages in sandbox** -- use whatever the default Daytona Python sandbox provides. If users need specific packages, they can `pip install` in their code.
- **No image/plot rendering** -- plots generated in the sandbox are not captured as images. Users see stdout/stderr only. Image support can be added later.
- **No streaming of code execution** -- the tool call blocks until execution completes, then returns the full result. Streaming is Phase 6.
