"""Daytona sandbox tool for code execution."""

import asyncio
import base64
import logging
import os
from datetime import UTC, datetime

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

logger = logging.getLogger(__name__)

# File extensions that should be stored as base64-encoded binary
_BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".svg",
    ".ico",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".mp3",
    ".wav",
    ".ogg",
    ".mp4",
    ".avi",
    ".mov",
    ".webm",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".pkl",
    ".pickle",
    ".npy",
    ".npz",
    ".parquet",
    ".feather",
}

IDLE_TIMEOUT_MINUTES = 15

# Module-level state for sandbox lifecycle management
_sandboxes: dict[str, object] = {}
_last_used: dict[str, datetime] = {}
_daytona = None


def _get_daytona():
    """Lazily initialize the Daytona client."""
    global _daytona
    if _daytona is None:
        from daytona_sdk import Daytona, DaytonaConfig

        config_kwargs = {"api_key": os.environ["DAYTONA_API_KEY"]}
        api_url = os.environ.get("DAYTONA_API_URL")
        if api_url:
            config_kwargs["api_url"] = api_url

        _daytona = Daytona(DaytonaConfig(**config_kwargs))
    return _daytona


def _cleanup_idle_sandboxes():
    """Remove sandboxes that have been idle longer than the timeout."""
    now = datetime.now(UTC)
    expired = [
        tid for tid, last_used in _last_used.items() if (now - last_used).total_seconds() > IDLE_TIMEOUT_MINUTES * 60
    ]
    for tid in expired:
        if tid in _sandboxes:
            try:
                _get_daytona().delete(_sandboxes.pop(tid))
                logger.info("Cleaned up idle sandbox for thread %s", tid)
            except Exception:
                logger.warning("Failed to delete sandbox for thread %s", tid, exc_info=True)
            _last_used.pop(tid, None)


def _patch_proxy_url(sandbox):
    """Override the toolbox proxy URL if DAYTONA_PROXY_URL is set.

    The Daytona API returns a toolboxProxyUrl that may not be reachable from
    this container (e.g. proxy.localhost). This allows overriding it.
    """
    proxy_url = os.environ.get("DAYTONA_PROXY_URL")
    if proxy_url and hasattr(sandbox, "_toolbox_api"):
        sandbox._toolbox_api._toolbox_base_url = proxy_url
        logger.info("Patched sandbox proxy URL to %s", proxy_url)


def _get_or_create_sandbox(thread_id: str):
    """Get an existing sandbox for a thread or create a new one.

    Uses a declarative image with uv pre-installed so that scripts with
    PEP 723 inline metadata (# /// script) can declare their own dependencies
    and have them resolved automatically via `uv run`.
    """
    from daytona_sdk import CreateSandboxFromImageParams, Image

    _cleanup_idle_sandboxes()

    if thread_id not in _sandboxes:
        image = Image.debian_slim("3.12").pip_install(["uv"])
        sandbox = _get_daytona().create(CreateSandboxFromImageParams(image=image))
        _patch_proxy_url(sandbox)
        _sandboxes[thread_id] = sandbox
        logger.info("Created sandbox for thread %s (with uv)", thread_id)

    _last_used[thread_id] = datetime.now(UTC)
    return _sandboxes[thread_id]


def is_sandbox_available() -> bool:
    """Check if the Daytona sandbox is configured (API key is set)."""
    return bool(os.environ.get("DAYTONA_API_KEY"))


def cleanup_sandbox(thread_id: str):
    """Clean up a specific sandbox (e.g. when a conversation is deleted)."""
    if thread_id in _sandboxes:
        try:
            _get_daytona().delete(_sandboxes.pop(thread_id))
            logger.info("Cleaned up sandbox for thread %s", thread_id)
        except Exception:
            logger.warning("Failed to delete sandbox for thread %s", thread_id, exc_info=True)
        _last_used.pop(thread_id, None)


async def cleanup_idle_sandboxes():
    """Async wrapper for idle sandbox cleanup."""
    await asyncio.to_thread(_cleanup_idle_sandboxes)


@tool
async def execute_python_code(
    code: str,
    *,
    runtime: ToolRuntime,
) -> Command:
    """Execute Python code in a sandboxed environment and return the output.

    Use this tool to run data analysis, computations, or any Python code.
    The sandbox persists across calls within the same conversation, so you
    can build on previous code executions.

    Args:
        code: Python code to execute.
    """
    thread_id = runtime.config.get("configurable", {}).get("thread_id", "default")

    def _run():
        sandbox = _get_or_create_sandbox(thread_id)

        # Snapshot files before execution to detect new output files
        try:
            pre_files = {f.name for f in sandbox.fs.list_files(".")}
        except Exception:
            pre_files = set()

        response = sandbox.process.code_run(code)

        # Discover new files created during execution
        new_files = {}
        try:
            post_files = sandbox.fs.list_files(".")
            for f in post_files:
                if f.is_dir or f.name in pre_files:
                    continue
                # Skip large files > 1MB
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

    # Build files state update from captured output files
    now = datetime.now(UTC).isoformat()
    files_update = {}
    for fpath, finfo in new_files.items():
        encoding = finfo["encoding"]
        raw = finfo["content"]
        if encoding == "base64":
            files_update[fpath] = {
                "content": [raw],
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

    update_dict: dict = {
        "messages": [
            ToolMessage(
                content=result,
                tool_call_id=runtime.tool_call_id,
            )
        ],
    }

    if files_update:
        update_dict["files"] = files_update

    return Command(update=update_dict)
