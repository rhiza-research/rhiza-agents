"""MCP tool loading for rhiza-agents."""

import asyncio
import logging
import os

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger(__name__)

MCP_SERVER_URL_DEFAULT = "http://localhost:8000/sse"

# Retry settings for waiting on MCP server readiness (e.g. in docker-compose)
MAX_RETRIES = 5
RETRY_DELAY_SECONDS = 3


async def get_mcp_tools() -> list[BaseTool]:
    """Load tools from the Sheerwater MCP server.

    Connects to the MCP server specified by MCP_SERVER_URL env var
    (default: http://localhost:8000/sse) and returns LangChain-compatible tools.

    Retries a few times to handle docker-compose startup ordering, where the
    MCP server may not be ready when this module is first imported.

    Returns an empty list if the MCP server is unreachable after all retries.
    """
    mcp_url = os.environ.get("MCP_SERVER_URL", MCP_SERVER_URL_DEFAULT)
    client = MultiServerMCPClient(
        {
            "sheerwater": {
                "url": mcp_url,
                "transport": "sse",
            }
        }
    )
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            tools = await client.get_tools()
            logger.info("Loaded %d MCP tools from %s", len(tools), mcp_url)
            return tools
        except Exception:
            if attempt < MAX_RETRIES:
                logger.info(
                    "MCP server at %s not ready (attempt %d/%d), retrying in %ds...",
                    mcp_url,
                    attempt,
                    MAX_RETRIES,
                    RETRY_DELAY_SECONDS,
                )
                await asyncio.sleep(RETRY_DELAY_SECONDS)
            else:
                logger.warning(
                    "Could not connect to MCP server at %s after %d attempts, starting without MCP tools",
                    mcp_url,
                    MAX_RETRIES,
                )
    return []
