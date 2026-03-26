"""File management tools for writing files to graph state and executing them in the sandbox."""

import asyncio
import base64
import logging
from datetime import UTC, datetime

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from .sandbox import _BINARY_EXTENSIONS

logger = logging.getLogger(__name__)

# Maximum file size in bytes (1MB)
_MAX_FILE_SIZE = 1_000_000


@tool
async def write_file(
    path: str,
    content: str,
    *,
    runtime: ToolRuntime,
) -> Command:
    """Write a file to the conversation's virtual filesystem.

    Use this to save code, data, or any text file that should be visible
    to the user. Files are persisted in the conversation and can be
    viewed, downloaded, or executed later.

    Args:
        path: File path (e.g., '/code/analysis.py', '/output/results.csv').
        content: The full text content of the file.
    """
    if not path.startswith("/"):
        path = "/" + path

    content_size = len(content.encode("utf-8"))
    if content_size > _MAX_FILE_SIZE:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=f"Error: File too large ({content_size} bytes). Maximum is {_MAX_FILE_SIZE} bytes.",
                        tool_call_id=runtime.tool_call_id,
                    )
                ],
            }
        )

    now = datetime.now(UTC).isoformat()
    lines = content.split("\n")

    file_data = {
        "content": lines,
        "source": "agent",
        "created_at": now,
        "modified_at": now,
    }

    result_msg = f"File written: {path} ({len(lines)} lines, {content_size} bytes)"

    # Note: file display is handled by file_written SSE event from tool_start,
    # not stream_writer, because loadFiles() would overwrite the immediate
    # display before the checkpoint saves.

    return Command(
        update={
            "messages": [ToolMessage(content=result_msg, tool_call_id=runtime.tool_call_id)],
            "files": {path: file_data},
        }
    )


@tool
async def run_file(
    path: str,
    *,
    runtime: ToolRuntime,
) -> Command:
    """Execute a Python file from the conversation's virtual filesystem in the sandbox.

    Reads the file content from state and runs it in the code sandbox.
    The file must already exist (written via write_file).

    Args:
        path: Path of the file to execute (e.g., '/code/analysis.py').
    """
    if not path.startswith("/"):
        path = "/" + path

    files = runtime.state.get("files", {})
    file_data = files.get(path)
    if not file_data:
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=f"Error: File not found: {path}",
                        tool_call_id=runtime.tool_call_id,
                    )
                ],
            }
        )

    content_lines = file_data.get("content", [])
    code = "\n".join(content_lines)

    if not code.strip():
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=f"Error: File is empty: {path}",
                        tool_call_id=runtime.tool_call_id,
                    )
                ],
            }
        )

    from .sandbox import _get_or_create_sandbox, is_sandbox_available

    if not is_sandbox_available():
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content="Error: Code sandbox is not available (DAYTONA_API_KEY not set).",
                        tool_call_id=runtime.tool_call_id,
                    )
                ],
            }
        )

    thread_id = runtime.config.get("configurable", {}).get("thread_id", "default")

    def _run():
        sandbox = _get_or_create_sandbox(thread_id)

        # Write script to sandbox and run via uv for PEP 723 dependency resolution
        filename = path.lstrip("/")
        sandbox.fs.upload_file(code.encode("utf-8"), filename)

        # Snapshot files after upload but before execution to detect new output files
        try:
            pre_files = {f.name for f in sandbox.fs.list_files(".")}
        except Exception:
            pre_files = set()

        response = sandbox.process.exec(f"uv run {filename}")

        # Discover new files created during execution
        new_files = {}
        try:
            post_files = sandbox.fs.list_files(".")
            for f in post_files:
                if f.is_dir or f.name in pre_files:
                    continue
                # Download new output files (skip large files > 1MB)
                if f.size and f.size > 1_000_000:
                    continue
                try:
                    content_bytes = sandbox.fs.download_file(f.name)
                    ext = "." + f.name.rsplit(".", 1)[-1].lower() if "." in f.name else ""
                    if ext in _BINARY_EXTENSIONS:
                        new_files[f"/{f.name}"] = {
                            "content": base64.b64encode(content_bytes).decode("ascii"),
                            "encoding": "base64",
                        }
                    else:
                        new_files[f"/{f.name}"] = {
                            "content": content_bytes.decode("utf-8", errors="replace"),
                            "encoding": "utf-8",
                        }
                except Exception:
                    pass
        except Exception:
            pass

        if response.exit_code != 0:
            return f"Error (exit code {response.exit_code}):\n{response.result}", new_files
        return response.result, new_files

    result, new_files = await asyncio.to_thread(_run)

    # Add any output files to state
    now = datetime.now(UTC).isoformat()
    files_update = {}
    for fpath, finfo in new_files.items():
        encoding = finfo["encoding"]
        raw = finfo["content"]
        if encoding == "base64":
            files_update[fpath] = {
                "content": [raw],  # Single base64 string as one "line"
                "source": "output",
                "encoding": "base64",
                "modified_at": now,
            }
        else:
            lines = raw.split("\n")
            files_update[fpath] = {
                "content": lines,
                "source": "output",
                "modified_at": now,
            }

    update_dict = {
        "messages": [
            ToolMessage(
                content=f"Execution output for {path}:\n{result}",
                tool_call_id=runtime.tool_call_id,
            )
        ],
    }

    if files_update:
        existing_files = runtime.state.get("files", {})
        update_dict["files"] = {**existing_files, **files_update}

    return Command(update=update_dict)
