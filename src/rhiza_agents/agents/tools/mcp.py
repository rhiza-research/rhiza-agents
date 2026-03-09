"""MCP tool loading via langchain-mcp-adapters."""

from langchain_mcp_adapters.client import MultiServerMCPClient


def create_mcp_client(mcp_server_url: str) -> MultiServerMCPClient:
    """Create an MCP client for the sheerwater server.

    The returned client must be used as an async context manager.
    Call client.get_tools() inside the context to get LangChain-compatible tools.
    """
    return MultiServerMCPClient(
        {
            "sheerwater": {
                "url": mcp_server_url,
                "transport": "sse",
            }
        }
    )
