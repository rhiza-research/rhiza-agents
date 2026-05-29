"""Agent config CRUD API routes.

Read, edit, and reset the agent's configuration (keyed by ``AGENT_ID``).
Create and delete are not supported.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request

from ..agents.graph import invalidate_graph_cache
from ..agents.registry import (
    AGENT_ID,
    get_default_configs,
    get_default_configs_by_id,
    merge_configs,
)
from ..agents.tools.sandbox import is_sandbox_available
from ..db.models import AgentConfig
from ..deps import _user_mcp_cache, get_db, get_mcp_tools, get_mcp_tools_by_server, get_user_id, require_auth

router = APIRouter(tags=["agents"])


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
    """Get the effective agent config for the current user.

    Returned as a one-element list for frontend compatibility.
    """
    user_id = get_user_id(request)
    effective = await _get_effective_configs(request, user_id)
    return _configs_to_api_response(effective)


@router.put("/api/agents/{agent_id}")
async def update_agent(request: Request, agent_id: str, user: dict = Depends(require_auth)):
    """Update the agent's config override."""
    db = get_db(request)
    user_id = get_user_id(request)
    body = await request.json()

    if agent_id != AGENT_ID:
        raise HTTPException(status_code=404, detail="Agent not found")

    config_data = get_default_configs_by_id()[AGENT_ID].model_dump()

    # Apply the update fields. `enabled` is intentionally not editable: the
    # agent is always on (disabling it would leave no buildable graph).
    for field in ("name", "system_prompt", "model", "tools", "vectorstore_ids"):
        if field in body:
            config_data[field] = body[field]
    config_data["id"] = agent_id
    if not str(config_data.get("model") or "").strip():
        raise HTTPException(status_code=400, detail="model must not be empty")

    # Validate
    config = AgentConfig(**config_data)

    await db.save_user_agent_config(user_id, agent_id, config.model_dump())
    invalidate_graph_cache()

    effective = await _get_effective_configs(request, user_id)
    return _configs_to_api_response(effective)


@router.post("/api/agents")
async def create_agent(request: Request, user: dict = Depends(require_auth)):
    """Creating agents is not supported."""
    raise HTTPException(status_code=405, detail="Creating agents is not supported")


@router.delete("/api/agents/{agent_id}")
async def delete_agent(request: Request, agent_id: str, user: dict = Depends(require_auth)):
    """Deleting the agent is not supported."""
    raise HTTPException(status_code=405, detail="Deleting the agent is not supported")


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
    # Add all skills (system + user) as tool types
    from ..agents.tools.skills import requires_sandbox as skill_requires_sandbox

    skills = await db.list_skills(user_id)
    for skill in skills:
        sid = skill["id"]
        name = skill["name"]
        available = bool(skill.get("enabled", True))
        tool_types.append(
            {
                "id": f"skill:{sid}",
                "name": f"{name} Skill",
                "available": available,
                "requires_sandbox": skill_requires_sandbox(skill),
            }
        )
    return tool_types


@router.get("/api/tools")
async def list_tools(request: Request, user: dict = Depends(require_auth)):
    """List available MCP tools."""
    mcp_tools = get_mcp_tools(request)
    return [{"name": t.name, "description": t.description} for t in mcp_tools]
