"""MCP tool loading via langchain-mcp-adapters."""

import logging

from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger(__name__)


def create_mcp_client(servers: dict[str, dict]) -> MultiServerMCPClient:
    """Create an MCP client for one or more servers.

    Args:
        servers: Dict of server_id -> {"url": ..., "transport": "sse"|"stdio"}

    The returned client must be used as an async context manager.
    Call client.get_tools() inside the context to get LangChain-compatible tools.
    """
    return MultiServerMCPClient(servers)


async def load_mcp_tools_for_server(url: str, transport: str = "sse") -> list:
    """Load tools from a single MCP server.

    Returns a list of LangChain-compatible tools, or an empty list on failure.
    """
    client = MultiServerMCPClient({"server": {"url": url, "transport": transport}})
    try:
        tools = await client.get_tools()
        return tools
    except Exception as e:
        logger.warning("Failed to load MCP tools from %s: %s", url, e)
        return []
