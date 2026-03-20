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
