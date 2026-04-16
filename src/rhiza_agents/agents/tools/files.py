"""File management tools for writing files to graph state and executing them in the sandbox."""

import asyncio
import base64
import logging
import shlex
from datetime import UTC, datetime

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from ...credentials import redact_output
from .sandbox import (
    _BINARY_EXTENSIONS,
    _normalize_sandbox_upload_path,
    resolve_credentials_or_error,
)

logger = logging.getLogger(__name__)

# Maximum file size in bytes (1MB)
_MAX_FILE_SIZE = 1_000_000


def _build_uv_run_cmd(filename: str, script_args: list[str] | None) -> str:
    """Build the ``uv run ...`` shell command with each arg shell-quoted.

    Pure helper — extracted so the command-building logic can be tested
    independently of the Daytona sandbox.
    """
    if not script_args:
        return f"uv run {filename}"
    suffix = " ".join(shlex.quote(a) for a in script_args)
    return f"uv run {filename} {suffix}"


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


def make_run_file(db=None):
    """Build the ``run_file`` tool with credential support.

    Like ``make_execute_python_code``, this is a factory because the tool
    needs the application database to resolve stored credentials at run
    time. The two tools share their credential semantics via
    ``resolve_credentials_or_error`` so the LLM sees consistent behavior.
    """

    @tool
    async def run_file(
        path: str,
        script_args: list[str] | None = None,
        credentials: list[dict] | None = None,
        *,
        runtime: ToolRuntime,
    ) -> Command:
        """Execute a Python file from the conversation's virtual filesystem in the sandbox.

        Reads the file content from state and runs it in the code sandbox
        via ``uv run`` (which honors PEP 723 inline script dependencies).
        The file must already exist (written via ``write_file`` or loaded
        into state by a skill activation).

        This is the preferred way to run any non-trivial code that the user
        should be able to review. Always go through ``write_file`` →
        ``run_file`` for scripts; never use ``execute_python_code`` to run
        code that lives in (or could live in) a file.

        Args:
            path: Path of the file to execute (e.g., '/code/analysis.py').
            script_args: Optional list of CLI arguments passed after the
                script path, e.g. ``["--date", "2026-02-15", "--region",
                "africa", "--output", "/tmp/out.zarr"]``. Each element is
                passed as a single token; no shell interpretation. Omit or
                pass ``None`` to run with no arguments.
            credentials: Optional list of materialization plans describing
                which stored secrets to make available to this run. Same
                shape as ``execute_python_code``'s ``credentials`` argument:

                    {"kind": "env_vars", "names": ["TAHMO_USERNAME", "TAHMO_PASSWORD"]}

                    {"kind": "file", "path": "~/.netrc",
                     "names": ["NASA_USERNAME", "NASA_PASSWORD"],
                     "content": "machine x login {NASA_USERNAME} password {NASA_PASSWORD}\\n"}

                When a skill's activation response lists required
                credential names (declared via a
                ``metadata.openclaw.requires.env`` block in its SKILL.md),
                wrap those names in an ``env_vars`` plan here.

                The user must approve the run before any credential is
                injected. Do not print, log, or echo credential values from
                your script — the system will redact verbatim occurrences
                from output as a backstop, and the user can see which secret
                names you requested in the approval card.
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
        materializations = credentials or []

        # Resolve credentials before touching the sandbox so a bad request
        # never has any side effects.
        resolved = await resolve_credentials_or_error(db, thread_id, materializations, runtime.tool_call_id)
        if isinstance(resolved, Command):
            return resolved
        env_vars, file_uploads, redaction_list = resolved

        def _run():
            sandbox = _get_or_create_sandbox(thread_id)

            # Apply file materializations (e.g. ~/.netrc) before running.
            # Always 0600 — these are credential files. Daytona's
            # ``fs.upload_file`` signature is (content_bytes, remote_path),
            # and remote_path is resolved against the sandbox's working
            # directory and does NOT expand ``~``, so the path is
            # normalized first; the chmod still uses the original logical
            # path because it runs in a shell that expands ``~``.
            for cred_path, content in file_uploads.items():
                upload_path = _normalize_sandbox_upload_path(cred_path)
                try:
                    sandbox.fs.upload_file(content.encode("utf-8"), upload_path)
                except Exception:
                    logger.warning("Failed to upload credential file %s", cred_path, exc_info=True)
                try:
                    sandbox.process.exec(f"chmod 600 {cred_path}")
                except Exception:
                    pass

            # Write the script itself to the sandbox.
            filename = _normalize_sandbox_upload_path(path)
            sandbox.fs.upload_file(code.encode("utf-8"), filename)

            # Snapshot files after upload but before execution to detect new output files
            try:
                pre_files = {f.name for f in sandbox.fs.list_files(".")}
            except Exception:
                pre_files = set()

            cmd = _build_uv_run_cmd(filename, script_args)

            # sandbox.process.exec accepts an env dict that's merged into
            # the process environment for this single command, which is
            # exactly the per-execution scoping we want for credentials.
            if env_vars:
                response = sandbox.process.exec(cmd, env=dict(env_vars))
            else:
                response = sandbox.process.exec(cmd)

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

            # Best-effort cleanup of credential files so they don't linger
            # between executions.
            for cred_path in file_uploads:
                try:
                    sandbox.process.exec(f"rm -f {cred_path}")
                except Exception:
                    pass

            if response.exit_code != 0:
                return f"Error (exit code {response.exit_code}):\n{response.result}", new_files
            return response.result, new_files

        result, new_files = await asyncio.to_thread(_run)

        # Backstop: scrub verbatim secret values from anything we return.
        result = redact_output(result, redaction_list)

        # Add any output files to state, with values redacted.
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
                redacted = redact_output(raw, redaction_list)
                lines = redacted.split("\n")
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

    return run_file
