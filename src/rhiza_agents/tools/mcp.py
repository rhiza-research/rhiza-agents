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

# Cache MCP tools to avoid reconnecting on every graph build
_tools_cache: dict[str, list[BaseTool]] = {}


async def get_mcp_tools() -> list[BaseTool]:
    """Load tools from the default Sheerwater MCP server.

    Connects to the MCP server specified by MCP_SERVER_URL env var
    (default: http://localhost:8000/sse) and returns LangChain-compatible tools.

    Retries a few times to handle docker-compose startup ordering, where the
    MCP server may not be ready when this module is first imported.

    Returns an empty list if the MCP server is unreachable after all retries.
    """
    mcp_url = os.environ.get("MCP_SERVER_URL", MCP_SERVER_URL_DEFAULT)
    return await get_mcp_tools_from_server("sheerwater", mcp_url, "sse")


async def get_mcp_tools_from_server(name: str, url: str, transport: str = "sse") -> list[BaseTool]:
    """Load tools from a specific MCP server.

    Args:
        name: Server name (used for caching and as MCP client key).
        url: Server URL.
        transport: Transport type ("sse" or "streamable_http").

    Returns:
        List of LangChain-compatible tools, or empty list on failure.
    """
    cache_key = f"{name}:{url}"
    if cache_key in _tools_cache:
        return _tools_cache[cache_key]

    client = MultiServerMCPClient({name: {"url": url, "transport": transport}})
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            tools = await client.get_tools()
            logger.info("Loaded %d MCP tools from %s (%s)", len(tools), name, url)
            _tools_cache[cache_key] = tools
            return tools
        except Exception:
            if attempt < MAX_RETRIES:
                logger.info(
                    "MCP server %s at %s not ready (attempt %d/%d), retrying in %ds...",
                    name,
                    url,
                    attempt,
                    MAX_RETRIES,
                    RETRY_DELAY_SECONDS,
                )
                await asyncio.sleep(RETRY_DELAY_SECONDS)
            else:
                logger.warning(
                    "Could not connect to MCP server %s at %s after %d attempts, starting without its tools",
                    name,
                    url,
                    MAX_RETRIES,
                )
    return []


async def get_all_mcp_tools(server_configs: list[dict] | None = None) -> list[BaseTool]:
    """Load tools from all configured MCP servers.

    Always includes the default sheerwater server. Additional servers
    are loaded from the provided configs.

    Args:
        server_configs: List of dicts with keys: name, url, transport.

    Returns:
        Aggregated list of tools from all servers.
    """
    all_tools = []

    # Always load default sheerwater tools
    all_tools.extend(await get_mcp_tools())

    # Load tools from additional configured servers
    for config in server_configs or []:
        tools = await get_mcp_tools_from_server(
            config["name"],
            config["url"],
            config.get("transport", "sse"),
        )
        all_tools.extend(tools)

    return all_tools


def invalidate_mcp_cache():
    """Clear the MCP tools cache (e.g., when server configs change)."""
    _tools_cache.clear()
