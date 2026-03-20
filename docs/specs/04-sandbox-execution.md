# Phase 4: Sandboxed Code Execution

**Status: Implemented**

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
from daytona_sdk import Daytona, DaytonaConfig, CreateSandboxBaseParams

# LangChain tool decorator and config injection
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
```

The `daytona-sdk` package should already be in `pyproject.toml` as an optional dependency (`sandbox` extra).

**Important SDK note:** The class is `CreateSandboxBaseParams`, NOT `CreateSandboxParams`. The response from `sandbox.process.code_run(code)` returns an object with `.result` (combined output string) and `.exit_code` (int). There are no separate `.output` / `.output_error` fields.

## Working Reference Implementation

A tested, working implementation exists in the deepagents repo:
- **File:** `../rhiza-agents-deepagents/src/rhiza_agents/tools/sandbox.py`

This implementation is verified working end-to-end. Port it directly rather than writing from scratch. Key patterns to preserve from that implementation are documented below.

## Implementation Details

### Modifications to `config.py`

Add new environment variables:

| Env Var | Required | Default | Purpose |
|---------|----------|---------|---------|
| `DAYTONA_API_KEY` | no | `""` | Daytona API key for sandbox creation. If empty, sandbox tool is unavailable. |
| `DAYTONA_API_URL` | no | SDK default | Daytona API base URL (only needed for non-default endpoints). |
| `DAYTONA_PROXY_URL` | no | `""` | Override for the sandbox proxy URL. Required when running in Docker — see "Proxy URL Fix" below. |

### `agents/tools/sandbox.py` -- Daytona Sandbox Tool

This module provides a LangChain tool that executes Python code in Daytona sandboxes.

**Port the working implementation from** the `rhiza-agents-deepagents` sibling repo (`src/rhiza_agents/tools/sandbox.py`). The key design patterns (verified working):

**1. Lazy Daytona client initialization** — A single `Daytona` client is initialized on first use and reused. Reads `DAYTONA_API_KEY` and optional `DAYTONA_API_URL` from env vars directly (not passed as args).

**2. Module-level sandbox state** — Simple dicts for sandbox objects and last-used timestamps, keyed by thread_id:

```python
_sandboxes: dict[str, object] = {}
_last_used: dict[str, datetime] = {}
_daytona = None
IDLE_TIMEOUT_MINUTES = 15
```

**3. RunnableConfig injection for thread_id** — The `@tool` function uses `*, config: RunnableConfig` to get the thread_id. This is automatically injected by LangGraph and NOT exposed to the LLM:

```python
@tool
async def execute_python_code(code: str, *, config: RunnableConfig) -> str:
    """Execute Python code in a sandboxed environment and return the output."""
    thread_id = config.get("configurable", {}).get("thread_id", "default")
```

**4. asyncio.to_thread for sync SDK calls** — The Daytona SDK is synchronous. Wrap the blocking calls:

```python
return await asyncio.to_thread(_run)
```

**5. Response format** — `sandbox.process.code_run(code)` returns an object with:
- `response.result` — combined output string (stdout/stderr)
- `response.exit_code` — integer exit code

There are NO separate `.output` / `.output_error` fields. Keep the output format simple:

```python
if response.exit_code != 0:
    return f"Error (exit code {response.exit_code}):\n{response.result}"
return response.result
```

**6. Availability check** — Provide `is_sandbox_available() -> bool` that checks if `DAYTONA_API_KEY` is set. Used by graph.py to conditionally add the tool.

**7. Idle cleanup** — Called before each sandbox creation. Removes sandboxes idle for more than 15 minutes via `daytona.delete(sandbox)`.

### Proxy URL Fix (Critical for Docker Deployments)

The Daytona API returns a `toolboxProxyUrl` in sandbox creation responses (e.g., `http://proxy.localhost:4000/toolbox`). This URL is often unreachable from Docker containers because `proxy.localhost` doesn't resolve.

The working implementation patches this after sandbox creation:

```python
def _patch_proxy_url(sandbox):
    proxy_url = os.environ.get("DAYTONA_PROXY_URL")
    if proxy_url and hasattr(sandbox, "_toolbox_api"):
        sandbox._toolbox_api._toolbox_base_url = proxy_url
```

Set `DAYTONA_PROXY_URL` in docker-compose.yml to the reachable proxy address (e.g., `http://<host-ip>:4000/toolbox`).

The Daytona API's `PROXY_DOMAIN` env var does NOT control this URL for existing deployments — it's baked into the database at initial setup. The SDK-side override is the only reliable fix.

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

Update the existing `_resolve_tools` function in `graph.py` to handle `sandbox:daytona`. The sandbox tool reads env vars directly (no API key passed through), so use `is_sandbox_available()` to check:

```python
def _resolve_tools(config: AgentConfig, mcp_tools: list) -> list:
    """Resolve tool identifiers to actual tool objects."""
    tools = []
    for tool_id in config.tools:
        if tool_id == "mcp:sheerwater":
            tools.extend(mcp_tools)
        elif tool_id == "sandbox:daytona":
            from .tools.sandbox import execute_python_code, is_sandbox_available
            if is_sandbox_available():
                tools.append(execute_python_code)
            # If no API key, silently skip -- agent works without tools
        else:
            logger.info("Tool type %s not yet implemented, skipping", tool_id)
    return tools
```

**LLM retry:** When creating `ChatAnthropic` model instances in `build_graph`, wrap them with `.with_retry()` to handle transient API failures (rate limits, 5xx errors):

```python
model = ChatAnthropic(model=config.model).with_retry(
    stop_after_attempt=3,
)
```

This applies to both the supervisor model and all worker agent models.

### Modifications to `main.py`

1. No changes needed for API key — the sandbox tool reads `DAYTONA_API_KEY` from env directly.
3. **Add `recursion_limit`** to the `graph.ainvoke()` call in `POST /api/chat` to prevent runaway agent loops:
   ```python
   result = await graph.ainvoke(
       {"messages": [HumanMessage(content=message)]},
       config={"configurable": {"thread_id": conversation_id}, "recursion_limit": 50},
   )
   ```
5. Start the sandbox cleanup background task in the lifespan:

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

6. When a conversation is deleted (DELETE /api/conversations/{id}), also call `cleanup_sandbox(conversation_id)`.

### Modifications to `docker-compose.yml`

Add Daytona env vars to the rhiza-agents service environment:
```yaml
DAYTONA_API_KEY: ${DAYTONA_API_KEY:-}
DAYTONA_API_URL: ${DAYTONA_API_URL:-}
DAYTONA_PROXY_URL: ${DAYTONA_PROXY_URL:-}
```

These read from the host environment. If `DAYTONA_API_KEY` is not set, sandbox tool is unavailable and code_runner responds conversationally. `DAYTONA_PROXY_URL` is needed when running in Docker — see "Proxy URL Fix" above.

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
| `docs/ARCHITECTURE.md` | Sandbox integration section, Daytona SDK usage |
| `docs/reference/daytona-integration.md` | Daytona SDK API reference, proxy URL fix, LangChain tool wrapping |
| `src/rhiza_agents/agents/graph.py` | Where to add tool resolution |
| `src/rhiza_agents/agents/registry.py` | Where to update code_runner config |
| `src/rhiza_agents/main.py` | Where to add background cleanup task |
| `../rhiza-agents-deepagents/src/rhiza_agents/tools/sandbox.py` | **Working implementation to port** |

For the Daytona SDK API, the key classes are:
- `Daytona(DaytonaConfig(api_key=..., api_url=...))` -- client constructor
- `daytona.create(CreateSandboxBaseParams(language="python"))` -- create a sandbox (NOT `CreateSandboxParams`)
- `sandbox.process.code_run(code)` -- execute code, returns object with `.result` (str) and `.exit_code` (int)
- `daytona.delete(sandbox)` -- destroy sandbox

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
12. Agent loops are capped at 50 recursion steps (graph raises `GraphRecursionError` if exceeded)
13. Transient LLM API failures (rate limits, 5xx) are retried up to 3 times automatically

## What NOT to Do

- **No file upload to sandbox** -- only code execution via the tool. Users cannot upload files to the sandbox.
- **No persistent sandbox state across conversations** -- each conversation gets its own sandbox. Starting a new conversation starts a fresh sandbox.
- **No self-hosted Daytona** -- use the hosted Daytona service only. No Docker-in-Docker setup.
- **No pre-installed packages in sandbox** -- use whatever the default Daytona Python sandbox provides. If users need specific packages, they can `pip install` in their code.
- **No image/plot rendering** -- plots generated in the sandbox are not captured as images. Users see stdout/stderr only. Image support can be added later.
- **No streaming of code execution** -- the tool call blocks until execution completes, then returns the full result. Streaming is Phase 6.

## Implementation Notes (Post-Implementation)

Implementation closely followed the spec. Key details:

### What was built

1. **`agents/tools/sandbox.py`** — Ported from deepagents reference implementation. Exports: `execute_python_code` (LangChain tool), `is_sandbox_available()`, `cleanup_sandbox(thread_id)`, `cleanup_idle_sandboxes()`.

2. **`config.py`** — Added `daytona_api_key`, `daytona_api_url`, `daytona_proxy_url` fields (all default to `""`). Note: the sandbox tool reads env vars directly; these config fields are for documentation/consistency.

3. **`graph.py`** — `_resolve_tools()` now handles `sandbox:daytona`. All `ChatAnthropic` instances (supervisor + workers) wrapped with `.with_retry(stop_after_attempt=3)`.

4. **`main.py`** changes:
   - Sandbox cleanup background task in lifespan (checks every 60s)
   - `recursion_limit: 50` on `graph.ainvoke()` in POST /api/chat
   - `cleanup_sandbox()` called on conversation delete
   - `_build_name_mappings()` and global `_tool_to_agent` now map `execute_python_code` → code_runner agent
   - New `GET /api/tool-types` endpoint returning tool availability

5. **`config.js`** — Fetches `/api/tool-types` on load. Tool checkboxes are now dynamic: enabled when `available=true`, disabled with "Not configured" badge when `available=false`. Save includes all checked tools (not just non-disabled).

6. **`chat.js`** — `renderCodeExecutionBlocks()` function pairs `execute_python_code` tool calls with their results from activity data. Renders syntax-highlighted code block + output block inline in the chat before the AI response message.

7. **`style.css`** — `.code-execution`, `.code-execution-header`, `.code-execution-code`, `.code-execution-output` classes.

### Deviations from spec

- **chat.html was NOT modified** — code execution blocks are rendered entirely by JavaScript (`renderCodeExecutionBlocks()` in chat.js), not by Jinja2 templates. Server-rendered pages show tool calls in the activity panel only; the inline code blocks appear on dynamic responses.
- **`main.py` does not modify `_process_messages()` return format** — tool calls and tool results were already included in the activity list. The JS pairs them up client-side for inline rendering.
- **`registry.py` kept the existing `_CODE_RUNNER_PROMPT`** (with `[THINKING]`/`[RESPONSE]` tags) rather than the simplified prompt in the spec.
