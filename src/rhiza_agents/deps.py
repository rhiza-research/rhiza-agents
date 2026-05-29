"""Shared FastAPI dependencies for route handlers."""

import json
import logging

from fastapi import HTTPException, Request

from .config import Config
from .db.sqlite import Database

logger = logging.getLogger(__name__)

# Cache of user MCP tools: server_id -> tools list
_user_mcp_cache: dict[str, list] = {}

# Cache of skill tools: skill_id -> BaseTool
_skill_cache: dict[str, "BaseTool"] = {}  # noqa: F821


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


async def mcp_tools_for_user(
    db: Database,
    system_tools_by_server: dict[str, list],
    user_id: str,
) -> tuple[dict[str, list], dict[str, str]]:
    """Resolve MCP tools and server names for a user (system + user servers).

    Request-free core used by both the web wrapper and the Slack connector.
    Returns (tools_by_server, server_names).
    """
    from .agents.tools.mcp import load_mcp_tools_for_server

    result = dict(system_tools_by_server)  # Start with system servers
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


async def get_mcp_tools_for_user(
    request: Request,
) -> tuple[dict[str, list], dict[str, str]]:
    """Get MCP tools and server names for the current request's user.

    Returns (tools_by_server, server_names) tuple.
    """
    return await mcp_tools_for_user(get_db(request), get_mcp_tools_by_server(request), get_user_id(request))


def invalidate_user_mcp_cache(server_id: str):
    """Clear cached tools for a specific user MCP server."""
    _user_mcp_cache.pop(server_id, None)


async def skill_tools_for_user(db: Database, user_id: str) -> dict[str, "BaseTool"]:  # noqa: F821
    """Resolve skill tools for a user (system + user skills), request-free.

    Returns a dict of skill_id -> BaseTool.
    """
    from .agents.tools.skills import create_skill_tool

    skills = await db.list_skills(user_id)

    result = {}
    for skill in skills:
        if not skill.get("enabled", True):
            continue
        sid = skill["id"]
        if sid not in _skill_cache:
            try:
                _skill_cache[sid] = create_skill_tool(skill)
            except (ValueError, Exception) as e:
                logger.warning("Failed to create tool for skill %s: %s", sid, e)
                continue
        result[sid] = _skill_cache[sid]
    return result


async def get_skill_tools_for_user(request: Request) -> dict[str, "BaseTool"]:  # noqa: F821
    """Get skill tools for the current request's user.

    Returns a dict of skill_id -> BaseTool.
    """
    return await skill_tools_for_user(get_db(request), get_user_id(request))


async def effective_agent_configs(db: Database, user_id: str):
    """Resolve a user's effective agent configs (defaults + DB overrides), request-free.

    Mirrors the web path's per-user config resolution so the Slack connector
    can build a graph for a mapped user without a request session.
    """
    from .agents.registry import get_default_configs, merge_configs

    defaults = get_default_configs()
    override_rows = await db.get_user_agent_configs(user_id)
    if not override_rows:
        return defaults
    overrides = [json.loads(row["config_json"]) for row in override_rows]
    return merge_configs(defaults, overrides)


def invalidate_skill_cache(skill_id: str | None = None):
    """Clear cached skill tools. If skill_id is None, clear all."""
    if skill_id is None:
        _skill_cache.clear()
    else:
        _skill_cache.pop(skill_id, None)


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
