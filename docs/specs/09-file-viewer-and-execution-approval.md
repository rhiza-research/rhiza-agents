# Phase 9: File Viewer and Code Execution Approval

## Goal

Users can see files the agent produces, download them, and optionally review code before it runs in the sandbox. Today, the sandbox tool executes code in one shot — the user sees the result but never the code until after it ran. Files written inside the sandbox are invisible unless the agent reads them back.

This phase adds:
1. A state-based virtual filesystem where agents write files visible in the UI
2. A file viewer panel for browsing, viewing, and downloading agent-produced files
3. A code execution approval flow where code is written to state first, then optionally reviewed before sandbox execution

## Prerequisites

Phase 8 (context management) complete. Sandbox execution (Phase 4) working.

## Problem

Three problems to solve:

1. **Files are invisible.** When the code runner writes `sha256_hash.txt` in the sandbox, the user has no way to see or download it except by asking the agent to `cat` it. There's no file browser.

2. **Code executes without review.** The agent writes code and runs it in the same tool call. The user has no opportunity to review, modify, or reject code before it hits the sandbox.

3. **Sandbox files don't persist.** Sandboxes are cleaned up after 15 minutes of inactivity. Any files inside are lost. There's no record of what was produced.

## Design

### State-Based Virtual Filesystem

Add a `files` dict to the LangGraph graph state. Files are stored as:

```python
{
    "/path/to/file.py": {
        "content": ["line 1", "line 2", ...],
        "created_at": "2026-03-20T12:00:00",
        "modified_at": "2026-03-20T12:00:00",
    }
}
```

This is the same structure used by the `deepagents` `StateBackend` (see `deepagents.backends.state` in the installed package for reference). Files persist in the LangGraph checkpoint, which is SQLite-backed — they survive restarts and live as long as the conversation.

The agent writes files to state via a new `write_file` tool. The sandbox tool, after execution, copies output files back into state so they're visible and persistent.

### File Viewer UI

A panel (sidebar or tab) in the chat UI that shows the current conversation's files. The UI reads files from the graph state via a new API endpoint.

Capabilities:
- Browse files by path
- View file contents with syntax highlighting
- Download files
- See which files changed during the current turn

### Code Execution Approval

Two modes, configurable per conversation or globally:

1. **Auto-execute** (current behavior): The agent writes code and runs it immediately. Good for trusted, low-risk tasks.

2. **Review before execute**: The agent writes code to a file in state (e.g., `/code/analysis.py`). The user sees it in the file viewer. The user can approve, request changes, or reject. On approval, the code is sent to the sandbox verbatim — no LLM re-interpretation.

The mode could be a simple toggle in the UI, similar to Claude Code's permission model ("ask before executing" vs "auto-run").

### How Execution Approval Works

In "review" mode:
1. Agent writes code to state via `write_file` tool
2. UI shows the file with an "Approve & Run" button
3. User reviews, optionally asks for changes (normal chat)
4. User clicks "Approve & Run" (or says "run it")
5. A new tool call sends the exact file contents from state to the sandbox
6. Results come back and are also written to state

The key property: what runs in the sandbox is exactly what the user approved. The LLM does not touch the code between approval and execution.

### Interaction with Existing Sandbox Tool

The current `execute_python_code` tool takes inline code and runs it immediately. This phase adds a second path:

- `write_file`: Writes code (or any file) to state. No execution.
- `run_file`: Reads a file from state and executes it in the sandbox. In "review" mode, this is only called after user approval.
- `execute_python_code`: Unchanged, still available for "auto-execute" mode.

The agent's system prompt and the execution mode determine which path is used.

### Interaction with Context Management (Phase 8)

Files in state are stored in the checkpoint alongside messages. The trimming in Phase 8 only trims messages — it doesn't touch the `files` dict. Summarization also only operates on messages. So files persist independently of message pruning.

However, large files will increase checkpoint size. Consider a size limit per file (e.g., 1MB) and a total limit per conversation.

## API

### New Endpoints

`GET /api/conversations/{id}/files` — List files in the conversation's state
Response: `[{"path": "/code/analysis.py", "size": 1234, "modified_at": "..."}]`

`GET /api/conversations/{id}/files/{path}` — Get file contents
Response: `{"path": "/code/analysis.py", "content": "import pandas as pd\n..."}`

`POST /api/conversations/{id}/files/{path}/run` — Execute a file from state in the sandbox (approval endpoint)
Response: streams execution output

### New Graph State

Add to the graph state schema:
```python
files: dict[str, dict]  # path -> file data
```

### New Tools

`write_file(path, content)` — Write a file to conversation state. Returns confirmation.

`run_file(path)` — Read a file from state and execute it in the sandbox. Returns execution output. Also writes any output files back to state.

## UI Changes

- Add a "Files" panel (collapsible, like the activity panel)
- File tree showing paths
- Click to view with syntax highlighting
- Download button per file
- "Approve & Run" button on code files when in review mode
- Execution mode toggle (auto-execute / review)

## What NOT to Do

- No real filesystem access from the agent — all files go through state
- No file editing in the UI — users give feedback via chat, the agent edits
- No git integration or version history — the checkpoint is the history
- No multi-file execution — run one file at a time
- No file sharing between conversations — files are per-conversation
- No changes to the database schema — files live in LangGraph checkpoint state

## Acceptance Criteria

1. Ask the agent to write a Python script — the file appears in the file viewer
2. Click the file to view its contents with syntax highlighting
3. Download the file
4. In review mode, the agent writes code but doesn't execute until approved
5. Click "Approve & Run" — the exact code shown executes in the sandbox
6. Execution output appears in chat and output files appear in the file viewer
7. Reload the page — files are still there (persisted in checkpoint)
8. Files survive message summarization (Phase 8 pruning doesn't affect them)
9. In auto-execute mode, behavior matches current Phase 4 behavior (no regression)

---

## Implementation Notes

### What was built

**Custom graph state schema** (`AgentGraphState` in `agents/graph.py`):
- Extends the default LangGraph `_OuterState` with a `files: Annotated[dict, _merge_files]` field
- `_merge_files` reducer merges file dicts additively (new paths added, existing paths overwritten)
- Passed to `create_supervisor()` via the `state_schema` parameter

**File tools** (`agents/tools/files.py`):
- `write_file(path, content)` -- writes file to graph state via `Command(update={"files": ..., "messages": ...})`
- `run_file(path)` -- reads file from state via `ToolRuntime.state`, executes in sandbox, returns output
- Both tools use `ToolRuntime` to access `tool_call_id` and graph state
- Both return `Command` objects to update state (required for tools that modify non-messages state)
- File size limit: 1MB per file

**Tool registration** (`agents/graph.py` `_resolve_tools`):
- `write_file` is always added when `sandbox:daytona` is in the agent's tools (writing to state doesn't need the sandbox)
- `run_file` and `execute_python_code` are only added when `DAYTONA_API_KEY` is configured
- Tool-to-agent mappings updated in both `_build_name_mappings()` and the lifespan startup

**API endpoints** (`main.py`):
- `GET /api/conversations/{id}/files` -- lists files from checkpoint state
- `GET /api/conversations/{id}/files/{path:path}` -- returns file content from checkpoint state
- No `POST .../run` endpoint was implemented; "Approve & Run" sends a chat message instead (simpler, leverages existing streaming pipeline)

**SSE event**:
- `files_changed` event emitted when `write_file` tool completes, triggers UI file list refresh

**File viewer UI** (`templates/chat.html`, `static/chat.js`, `static/style.css`):
- Collapsible "Files" panel (right side, alongside Activity panel)
- "Files" toggle button in chat header with `has-files` indicator
- File list view with path and size
- File detail view with syntax highlighting via highlight.js (reuses existing CDN import)
- Download button (client-side blob download)
- "Approve & Run" button visible on code files (.py, .js, .ts, .sh, .bash) when review mode is enabled
- Panel state persisted in localStorage

**Execution mode toggle**:
- "Review code" checkbox in chat header
- Persisted in localStorage (`execReviewMode`)
- When enabled, code files show "Approve & Run" button in file viewer
- "Approve & Run" sends `"Run the file /path/to/file"` as a chat message, which the agent processes via the `run_file` tool

### Deviations from spec

1. **No `POST /api/conversations/{id}/files/{path}/run` endpoint**: The spec proposed a dedicated execution endpoint. Instead, "Approve & Run" sends a chat message. This is simpler and more consistent -- the agent decides whether to use `run_file`, and the execution flows through the normal streaming pipeline with proper activity panel integration.

2. **No sandbox output file copying**: The spec mentioned copying output files from the sandbox back to state after `execute_python_code` runs. This was not implemented because the Daytona SDK's `code_run` only returns stdout/stderr -- there's no API to enumerate files created during execution. The `run_file` tool similarly only returns execution output. Users who want files persisted should have the agent use `write_file` explicitly.

3. **Execution mode is per-browser, not per-conversation**: The toggle is stored in localStorage, not in the conversation or user settings DB. This keeps it simple with no schema changes.

### Key technical details

- `ToolRuntime` from `langgraph.prebuilt` provides `state`, `tool_call_id`, and `config` to tools without exposing them to the LLM's tool-calling interface
- `Command` from `langgraph.types` lets tools return state updates alongside a `ToolMessage` response
- The `files` reducer in the state schema handles merging -- each `Command` update only contains the new/changed files, and the reducer merges them into the existing dict

### Remaining work

All items below are done.

#### 1. Streaming handler (DONE)

Switched from `astream_events(version="v2")` to `graph.astream(stream_mode=["updates", "messages", "custom"], version="v2", subgraphs=True)`. The `"updates"` stream emits `__interrupt__` when HITL pauses execution. `"messages"` stream provides token-by-token output with `langgraph_node` metadata. `"custom"` stream handles `stream_writer` output from tools.

#### 2. Resume endpoint (DONE)

`POST /api/chat/resume` accepts `{conversation_id, decision, message}`. Builds `Command(resume={"decisions": [...]})` and streams the resumed graph execution back as SSE events (same format as `/api/chat/stream`).

#### 3. Frontend interrupt UI (DONE)

`chat.js` handles `interrupt` SSE events by rendering an approval card with tool name, args, Approve/Reject buttons. Approve/Reject calls `/api/chat/resume` and pipes the response through the same `handleStreamEvent` logic. Frontend sends `execution_mode` (from the review checkbox) with each `/api/chat/stream` request.

#### 4. Execution mode enforcement (DONE)

The streaming handler in `main.py` now checks `body.execution_mode`. In "auto" mode, when HITL middleware produces an `__interrupt__`, the handler automatically resumes with `Command(resume={"decisions": [{"type": "approve"}]})` via a while loop around `graph.astream()`. In "review" mode, interrupts are sent to the frontend as before. This avoids custom middleware or multiple graph variants — the built-in `HumanInTheLoopMiddleware` is used as-is.

#### 5. Stream writer for file events (DONE)

`write_file` tool now calls `runtime.stream_writer({"type": "files_changed"})` to emit file change events via the `"custom"` stream mode. Removed the manual `files_changed` emission from both streaming handlers in `main.py` that checked for `write_file`/`run_file` ToolMessages in updates. The existing `custom` chunk handler in the streaming loop forwards these events to the client.

#### 6. Agent name mapping in streaming (DONE)

The streaming handler now reads `lc_agent_name` from message metadata (with fallback to `langgraph_node`). `lc_agent_name` contains the agent's `name` parameter from `create_agent` (e.g., `"data_analyst"`), which maps directly to display names via the `agent_names` dict. Previously it used `langgraph_node` which contains internal node names like `"model"` or `"tools"`.
