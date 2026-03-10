# Phase 3: Sandbox Execution + Middleware

## Goal

The agent can execute Python code in Daytona sandboxes. Users ask for code execution, the agent (or a code_runner subagent) writes Python code, executes it in a hosted sandbox, and returns the results. Additionally, middleware is added for production resilience: LLM retry, tool call limits, and context management.

## Prerequisites

Phase 2 must be complete:
- Deep agent with MCP tools, served by LangGraph Server
- deep-agents-ui fork with NextAuth.js + Keycloak auth
- Full stack running via Docker Compose

## What You're Building

1. A Daytona sandbox tool that executes Python code and returns stdout/stderr/exit code
2. A code_runner subagent with the sandbox tool (for context isolation)
3. Middleware: retry, tool call limits, summarization

## What You're NOT Building

- No file upload to sandbox — only code execution via the tool
- No persistent sandbox state across threads — each thread gets its own sandbox
- No self-hosted Daytona — use the hosted service only
- No image/plot rendering — stdout/stderr only

## Key Packages (Python)

| Package | Purpose |
|---------|---------|
| `daytona-sdk` | Hosted code execution sandbox |
| `langchain` middleware | Summarization, retry, tool limits |

## Implementation Details

### Daytona Sandbox Tool

Create a LangChain tool that:
1. Gets or creates a sandbox for the current thread (one sandbox per conversation)
2. Executes Python code in the sandbox
3. Returns stdout, stderr, and exit code
4. Manages sandbox lifecycle (idle timeout, cleanup)

The tool needs the thread_id to maintain one sandbox per conversation. Use LangChain's `RunnableConfig` injection to access the thread_id:

```python
from langchain_core.runnables import RunnableConfig

@tool
async def execute_python_code(code: str, *, config: RunnableConfig) -> str:
    thread_id = config.get("configurable", {}).get("thread_id", "default")
    # ... get or create sandbox for this thread
```

Sandbox lifecycle:
- Module-level dict mapping thread_id → sandbox instance
- 15-minute idle timeout
- Background cleanup task
- Cleanup on thread deletion (if LangGraph Server supports hooks)

### Code Runner Subagent

Add a `code_runner` subagent to the deep agent configuration:
- Dedicated system prompt for code execution
- Has the Daytona sandbox tool
- Optionally a different (cheaper/faster) model for code tasks

The main agent delegates code execution requests to this subagent, keeping the main agent's context clean.

### Middleware

Add middleware to `create_deep_agent()`:

1. **ModelRetryMiddleware** — Retries LLM calls on transient failures (rate limits, 5xx) with exponential backoff, up to 3 attempts

2. **ToolCallLimitMiddleware** — Caps tool calls per invocation to prevent runaway loops (e.g., 50 calls)

3. **SummarizationMiddleware** — Automatically summarizes conversation history when approaching token limits, preserving recent messages while compressing older context

Check the `deepagents` and `langchain` middleware documentation for exact configuration. If `create_deep_agent()` has built-in context management, the summarization middleware may not be needed separately.

### Environment Variables

| Env Var | Required | Default | Purpose |
|---------|----------|---------|---------|
| `DAYTONA_API_KEY` | no | `""` | Daytona API key. If empty, sandbox tool is unavailable. |

If `DAYTONA_API_KEY` is not set, the code_runner subagent should still exist but respond conversationally without code execution.

## Reference

For the Daytona SDK API:
- `Daytona(DaytonaConfig(api_key=...))` — client constructor
- `daytona.create(CreateSandboxParams(language="python"))` — create a sandbox
- `sandbox.process.code_run(code)` — execute code, returns result with `.result`, `.output`, `.output_error`, `.exit_code`
- `daytona.delete(sandbox)` — destroy sandbox

Check the installed `daytona-sdk` package for exact API signatures — the SDK may have changed.

For middleware:
- Check `langchain` middleware docs for `SummarizationMiddleware`, `ModelRetryMiddleware`, `ToolCallLimitMiddleware`
- Check `create_deep_agent()` docs for built-in middleware support

## Acceptance Criteria

1. Ask "write Python code to calculate the first 20 fibonacci numbers" → agent delegates to code_runner, code executes in sandbox, results appear
2. Ask a follow-up "now make it a generator" → code_runner reuses the same sandbox
3. After 15 minutes of inactivity, the sandbox is cleaned up
4. If `DAYTONA_API_KEY` is not set, code_runner responds conversationally without execution
5. Transient LLM API failures are retried automatically (verify in logs)
6. Agent loops are capped — a runaway tool call loop stops after the limit
7. Long conversations don't crash — summarization kicks in when context gets large

## What NOT to Do

- Do not build custom context management logic — use the middleware
- Do not build custom retry logic — use the middleware
- Do not add image/plot capture — stdout/stderr only for now
- Do not add file upload to sandboxes — code execution only
