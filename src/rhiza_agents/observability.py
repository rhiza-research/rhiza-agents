"""Langfuse observability integration.

Disabled (every accessor returns None) when LANGFUSE_PUBLIC_KEY is not set.
"""

import logging
import os
import secrets

logger = logging.getLogger(__name__)


def langfuse_enabled() -> bool:
    """True iff a Langfuse public key is configured."""
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY"))


def new_trace_id() -> str:
    """Generate an OpenTelemetry-compatible 32-char hex trace id."""
    return secrets.token_hex(16)


def make_langfuse_handler(trace_id: str | None = None):
    """Build a fresh Langfuse LangChain CallbackHandler bound to a trace id.

    A new handler is constructed per call so that the trace id can be set via
    `trace_context` (the only way to inject a custom trace id in the v4 SDK).
    Returns None when Langfuse is disabled or fails to initialize.
    """
    if not langfuse_enabled():
        return None
    try:
        from langfuse.langchain import CallbackHandler

        if trace_id:
            return CallbackHandler(trace_context={"trace_id": trace_id})
        return CallbackHandler()
    except Exception as e:
        logger.warning("Failed to construct Langfuse handler: %s", e)
        return None


def get_langfuse_client():
    """Return the Langfuse SDK client, or None when disabled."""
    if not langfuse_enabled():
        return None
    try:
        from langfuse import get_client

        return get_client()
    except Exception as e:
        logger.warning("Failed to get Langfuse client: %s", e)
        return None
