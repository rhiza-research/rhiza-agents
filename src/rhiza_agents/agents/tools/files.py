"""File management tools for writing files to graph state and executing them in the sandbox."""

import asyncio
import logging
from datetime import UTC, datetime

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

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
        "created_at": now,
        "modified_at": now,
    }

    result_msg = f"File written: {path} ({len(lines)} lines, {content_size} bytes)"

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
        response = sandbox.process.code_run(code)
        if response.exit_code != 0:
            return f"Error (exit code {response.exit_code}):\n{response.result}"
        return response.result

    result = await asyncio.to_thread(_run)

    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=f"Execution output for {path}:\n{result}",
                    tool_call_id=runtime.tool_call_id,
                )
            ],
        }
    )
