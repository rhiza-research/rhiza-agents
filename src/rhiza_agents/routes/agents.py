"""Agent config CRUD API routes."""

import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request

from ..agents.graph import invalidate_graph_cache
from ..agents.registry import get_default_configs, get_default_configs_by_id, merge_configs
from ..agents.tools.sandbox import is_sandbox_available
from ..db.models import AgentConfig
from ..deps import _user_mcp_cache, get_db, get_mcp_tools, get_mcp_tools_by_server, get_user_id, require_auth

router = APIRouter(tags=["agents"])

_AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


async def _get_effective_configs(request: Request, user_id: str) -> list[AgentConfig]:
    """Get effective agent configs for a user (defaults + overrides, merged)."""
    db = get_db(request)
    defaults = get_default_configs()
    override_rows = await db.get_user_agent_configs(user_id)
    if not override_rows:
        return defaults
    overrides = [json.loads(row["config_json"]) for row in override_rows]
    return merge_configs(defaults, overrides)


def _configs_to_api_response(configs: list[AgentConfig]) -> list[dict]:
    """Convert configs to the API response format with is_default field."""
    default_ids = set(get_default_configs_by_id().keys())
    result = []
    for c in configs:
        d = c.model_dump()
        d["is_default"] = c.id in default_ids
        result.append(d)
    return result


@router.get("/api/agents")
async def get_agents(request: Request, user: dict = Depends(require_auth)):
    """Get effective agent configs for the current user."""
    db = get_db(request)
    user_id = get_user_id(request)
    effective = await _get_effective_configs(request, user_id)
    # Also include disabled default agents that have overrides
    override_rows = await db.get_user_agent_configs(user_id)
    disabled_overrides = []
    effective_ids = {c.id for c in effective}
    for row in override_rows:
        parsed = json.loads(row["config_json"])
        if parsed.get("id") not in effective_ids:
            c = AgentConfig(**parsed)
            disabled_overrides.append(c)
    all_configs = list(effective) + disabled_overrides
    return _configs_to_api_response(all_configs)


@router.put("/api/agents/{agent_id}")
async def update_agent(request: Request, agent_id: str, user: dict = Depends(require_auth)):
    """Update an agent config override."""
    db = get_db(request)
    user_id = get_user_id(request)
    body = await request.json()

    # Build the full AgentConfig to validate
    defaults_by_id = get_default_configs_by_id()
    base = defaults_by_id.get(agent_id)
    if base:
        config_data = base.model_dump()
    else:
        # Check if it's a custom agent override
        existing = await db.get_user_agent_config(user_id, agent_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Agent not found")
        config_data = json.loads(existing["config_json"])

    # Apply the update fields
    for field in ("name", "system_prompt", "model", "tools", "enabled", "vectorstore_ids"):
        if field in body:
            config_data[field] = body[field]
    config_data["id"] = agent_id

    # Validate
    config = AgentConfig(**config_data)

    # Prevent disabling supervisor
    if config.type == "supervisor" and not config.enabled:
        raise HTTPException(status_code=400, detail="Cannot disable the supervisor agent")

    await db.save_user_agent_config(user_id, agent_id, config.model_dump())
    invalidate_graph_cache()

    effective = await _get_effective_configs(request, user_id)
    override_rows = await db.get_user_agent_configs(user_id)
    effective_ids = {c.id for c in effective}
    disabled = []
    for row in override_rows:
        parsed = json.loads(row["config_json"])
        if parsed.get("id") not in effective_ids:
            disabled.append(AgentConfig(**parsed))
    return _configs_to_api_response(list(effective) + disabled)


@router.post("/api/agents")
async def create_agent(request: Request, user: dict = Depends(require_auth)):
    """Create a new custom agent."""
    db = get_db(request)
    user_id = get_user_id(request)
    body = await request.json()

    agent_id = body.get("id", "")
    if not agent_id or not _AGENT_ID_PATTERN.match(agent_id):
        raise HTTPException(
            status_code=400, detail="Agent ID must be alphanumeric with underscores, starting with a letter"
        )

    # Check uniqueness
    defaults_by_id = get_default_configs_by_id()
    if agent_id in defaults_by_id:
        raise HTTPException(status_code=400, detail="Agent ID conflicts with a default agent")
    existing = await db.get_user_agent_config(user_id, agent_id)
    if existing:
        raise HTTPException(status_code=400, detail="Agent ID already exists")

    config = AgentConfig(
        id=agent_id,
        name=body.get("name", agent_id),
        type="worker",
        system_prompt=body.get("system_prompt", ""),
        model=body.get("model", "claude-sonnet-4-20250514"),
        tools=body.get("tools", []),
    )

    await db.save_user_agent_config(user_id, agent_id, config.model_dump())
    invalidate_graph_cache()

    effective = await _get_effective_configs(request, user_id)
    return _configs_to_api_response(effective)


@router.delete("/api/agents/{agent_id}")
async def delete_agent(request: Request, agent_id: str, user: dict = Depends(require_auth)):
    """Disable a default agent or delete a custom agent."""
    db = get_db(request)
    user_id = get_user_id(request)
    defaults_by_id = get_default_configs_by_id()

    # Cannot delete supervisor
    default = defaults_by_id.get(agent_id)
    if default and default.type == "supervisor":
        raise HTTPException(status_code=400, detail="Cannot disable the supervisor agent")

    if agent_id in defaults_by_id:
        # Default agent: save override with enabled=false
        config = defaults_by_id[agent_id].model_copy(update={"enabled": False})
        await db.save_user_agent_config(user_id, agent_id, config.model_dump())
    else:
        # Custom agent: delete the row entirely
        existing = await db.get_user_agent_config(user_id, agent_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Agent not found")
        await db.delete_user_agent_config(user_id, agent_id)

    invalidate_graph_cache()

    effective = await _get_effective_configs(request, user_id)
    override_rows = await db.get_user_agent_configs(user_id)
    effective_ids = {c.id for c in effective}
    disabled = []
    for row in override_rows:
        parsed = json.loads(row["config_json"])
        if parsed.get("id") not in effective_ids:
            disabled.append(AgentConfig(**parsed))
    return _configs_to_api_response(list(effective) + disabled)


@router.post("/api/agents/reset")
async def reset_agents(request: Request, user: dict = Depends(require_auth)):
    """Reset all agent configs to defaults."""
    db = get_db(request)
    user_id = get_user_id(request)
    await db.delete_all_user_agent_configs(user_id)
    invalidate_graph_cache()
    return _configs_to_api_response(get_default_configs())


@router.get("/api/tool-types")
async def list_tool_types(request: Request, user: dict = Depends(require_auth)):
    """List available tool types and their availability status."""
    db = get_db(request)
    user_id = get_user_id(request)
    system_tools = get_mcp_tools_by_server(request)
    tool_types = [
        {"id": "sandbox:daytona", "name": "Code Sandbox (Daytona)", "available": is_sandbox_available()},
    ]
    # Add all MCP servers (system + user) as tool types
    servers = await db.list_mcp_servers(user_id)
    for server in servers:
        sid = server["id"]
        tool_types.append(
            {
                "id": f"mcp:{sid}",
                "name": f"{server['name']} MCP Tools",
                "available": sid in system_tools or sid in _user_mcp_cache,
            }
        )
    return tool_types


@router.get("/api/tools")
async def list_tools(request: Request, user: dict = Depends(require_auth)):
    """List available MCP tools."""
    mcp_tools = get_mcp_tools(request)
    return [{"name": t.name, "description": t.description} for t in mcp_tools]
