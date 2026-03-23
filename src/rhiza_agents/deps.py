"""Shared FastAPI dependencies for route handlers."""

import logging

from fastapi import HTTPException, Request

from .config import Config
from .db.sqlite import Database

logger = logging.getLogger(__name__)

# Cache of user MCP tools: server_id -> tools list
_user_mcp_cache: dict[str, list] = {}


def get_db(request: Request) -> Database:
    """Get the database instance from app state."""
    return request.app.state.db


def get_config(request: Request) -> Config:
    """Get the application config from app state."""
    return request.app.state.config


def get_checkpointer(request: Request):
    """Get the LangGraph checkpointer from app state."""
    return request.app.state.checkpointer


def get_mcp_tools(request: Request) -> list:
    """Get the flat list of system MCP tools."""
    return request.app.state.mcp_tools


def get_mcp_tools_by_server(request: Request) -> dict[str, list]:
    """Get the server_id -> tools mapping for system MCP servers."""
    return request.app.state.mcp_tools_by_server


def get_vectorstore_manager(request: Request):
    """Get the vectorstore manager from app state."""
    return request.app.state.vectorstore_manager


def require_auth(request: Request):
    """Dependency that requires an authenticated user session."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def get_user_id(request: Request) -> str:
    """Extract user ID from session."""
    return request.session.get("user", {}).get("sub", "")


def get_user_name(request: Request) -> str:
    """Extract display name from session."""
    return request.session.get("user", {}).get("preferred_username", "User")


async def get_mcp_tools_for_user(
    request: Request,
) -> tuple[dict[str, list], dict[str, str]]:
    """Get MCP tools and server names for the current user (system + user servers).

    Returns (tools_by_server, server_names) tuple.
    """
    from .agents.tools.mcp import load_mcp_tools_for_server

    db = get_db(request)
    user_id = get_user_id(request)
    system_tools = get_mcp_tools_by_server(request)

    result = dict(system_tools)  # Start with system servers
    names: dict[str, str] = {}
    user_servers = await db.list_mcp_servers(user_id)
    for server in user_servers:
        sid = server["id"]
        names[sid] = server["name"]
        if server.get("user_id") is None:
            continue  # System server tools already in result
        if not server.get("enabled", True):
            continue
        if sid not in _user_mcp_cache:
            _user_mcp_cache[sid] = await load_mcp_tools_for_server(server["url"], server.get("transport", "sse"))
        if _user_mcp_cache[sid]:
            result[sid] = _user_mcp_cache[sid]
    return result, names


def invalidate_user_mcp_cache(server_id: str):
    """Clear cached tools for a specific user MCP server."""
    _user_mcp_cache.pop(server_id, None)


async def is_chat_logging_enabled(request: Request, user_id: str) -> bool:
    """Check if chat event logging is enabled for a user."""
    config = get_config(request)
    db = get_db(request)
    mode = config.chat_event_logging
    if mode == "false":
        return False
    user_pref = await db.get_user_setting(user_id, "chat_event_logging")
    if mode == "true":
        return user_pref != "false"
    if mode == "opt-in":
        return user_pref == "true"
    return False
