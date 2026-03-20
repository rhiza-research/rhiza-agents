"""Daytona sandbox tool for code execution."""

import asyncio
import logging
import os
from datetime import UTC, datetime

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

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
    """Get an existing sandbox for a thread or create a new one."""
    from daytona_sdk import CreateSandboxBaseParams

    _cleanup_idle_sandboxes()

    if thread_id not in _sandboxes:
        sandbox = _get_daytona().create(CreateSandboxBaseParams(language="python"))
        _patch_proxy_url(sandbox)
        _sandboxes[thread_id] = sandbox
        logger.info("Created sandbox for thread %s", thread_id)

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
async def execute_python_code(code: str, *, config: RunnableConfig) -> str:
    """Execute Python code in a sandboxed environment and return the output.

    Use this tool to run data analysis, computations, or any Python code.
    The sandbox persists across calls within the same conversation, so you
    can build on previous code executions.

    Args:
        code: Python code to execute.

    Returns:
        The stdout/stderr output of the code execution, or an error message.
    """
    thread_id = config.get("configurable", {}).get("thread_id", "default")

    def _run():
        sandbox = _get_or_create_sandbox(thread_id)
        response = sandbox.process.code_run(code)
        if response.exit_code != 0:
            return f"Error (exit code {response.exit_code}):\n{response.result}"
        return response.result

    return await asyncio.to_thread(_run)
