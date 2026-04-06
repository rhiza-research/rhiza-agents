"""Langfuse observability integration.

Provides a singleton CallbackHandler for LangChain/LangGraph tracing.
Disabled (returns None) when LANGFUSE_PUBLIC_KEY is not set.
"""

import logging
import os

logger = logging.getLogger(__name__)

_handler = None
_initialized = False


def get_langfuse_handler():
    """Return a Langfuse LangChain CallbackHandler, or None if not configured.

    The handler is a singleton — subsequent calls return the same instance.
    Per-trace metadata (user_id, session_id) is set on the run config, not here.
    """
    global _handler, _initialized
    if _initialized:
        return _handler
    _initialized = True

    if not os.environ.get("LANGFUSE_PUBLIC_KEY"):
        logger.info("Langfuse disabled: LANGFUSE_PUBLIC_KEY not set")
        return None

    try:
        from langfuse.langchain import CallbackHandler

        _handler = CallbackHandler()
        logger.info("Langfuse callback handler initialized (host=%s)", os.environ.get("LANGFUSE_HOST", "default"))
    except Exception as e:
        logger.warning("Failed to initialize Langfuse handler: %s", e)
        _handler = None
    return _handler
