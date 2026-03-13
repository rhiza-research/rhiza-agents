"""FastAPI sidecar API for agent config and MCP server management."""

import json
import logging
import os
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from rhiza_agents.agents.registry import get_default_configs, get_default_configs_by_id, merge_configs
from rhiza_agents.db.database import Database
from rhiza_agents.db.models import AgentConfig
from rhiza_agents.tools.mcp import get_all_mcp_tools

logger = logging.getLogger(__name__)

_AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")

db: Database | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and clean up resources."""
    global db
    db_path = os.environ.get("CONFIG_DB_PATH", "./config.db")
    db = Database(f"sqlite:///{db_path}")
    await db.connect()
    logger.info("Config API started, database at %s", db_path)
    yield
    await db.disconnect()


app = FastAPI(title="rhiza-agents config API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _get_user_id(request: Request) -> str:
    """Extract user ID from request headers."""
    user_id = request.headers.get("X-User-Id", "default")
    return user_id


async def _get_effective_configs(user_id: str) -> list[AgentConfig]:
    """Get effective agent configs for a user (defaults + overrides, merged)."""
    defaults = get_default_configs()
    override_rows = await db.get_user_agent_configs(user_id)
    if not override_rows:
        return defaults
    overrides = [json.loads(row["config_json"]) for row in override_rows]
    return merge_configs(defaults, overrides)


def _configs_to_api_response(configs: list[AgentConfig]) -> list[dict]:
    """Convert configs to API response format with is_default field."""
    default_ids = set(get_default_configs_by_id().keys())
    result = []
    for c in configs:
        d = c.model_dump()
        d["is_default"] = c.id in default_ids
        result.append(d)
    return result


# --- Agent Config API ---


@app.get("/api/agents")
async def get_agents(request: Request):
    """Get effective agent configs for the current user."""
    user_id = _get_user_id(request)
    effective = await _get_effective_configs(user_id)
    # Also include disabled default agents that have overrides
    override_rows = await db.get_user_agent_configs(user_id)
    disabled_overrides = []
    effective_ids = {c.id for c in effective}
    for row in override_rows:
        parsed = json.loads(row["config_json"])
        if parsed.get("id") not in effective_ids:
            disabled_overrides.append(AgentConfig(**parsed))
    all_configs = list(effective) + disabled_overrides
    return _configs_to_api_response(all_configs)


@app.post("/api/agents")
async def create_agent(request: Request):
    """Create a new custom agent."""
    user_id = _get_user_id(request)
    body = await request.json()

    agent_id = body.get("id", "")
    if not agent_id or not _AGENT_ID_PATTERN.match(agent_id):
        raise HTTPException(
            status_code=400, detail="Agent ID must be alphanumeric with underscores, starting with a letter"
        )

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

    effective = await _get_effective_configs(user_id)
    return _configs_to_api_response(effective)


@app.put("/api/agents/{agent_id}")
async def update_agent(request: Request, agent_id: str):
    """Update an agent config override."""
    user_id = _get_user_id(request)
    body = await request.json()

    defaults_by_id = get_default_configs_by_id()
    base = defaults_by_id.get(agent_id)
    if base:
        config_data = base.model_dump()
    else:
        existing = await db.get_user_agent_config(user_id, agent_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Agent not found")
        config_data = json.loads(existing["config_json"])

    for field in ("name", "system_prompt", "model", "tools", "enabled"):
        if field in body:
            config_data[field] = body[field]
    config_data["id"] = agent_id

    config = AgentConfig(**config_data)

    await db.save_user_agent_config(user_id, agent_id, config.model_dump())

    # Return all configs including disabled
    effective = await _get_effective_configs(user_id)
    override_rows = await db.get_user_agent_configs(user_id)
    effective_ids = {c.id for c in effective}
    disabled = []
    for row in override_rows:
        parsed = json.loads(row["config_json"])
        if parsed.get("id") not in effective_ids:
            disabled.append(AgentConfig(**parsed))
    return _configs_to_api_response(list(effective) + disabled)


@app.delete("/api/agents/{agent_id}")
async def delete_agent(request: Request, agent_id: str):
    """Disable a default agent or delete a custom agent."""
    user_id = _get_user_id(request)
    defaults_by_id = get_default_configs_by_id()

    if agent_id in defaults_by_id:
        config = defaults_by_id[agent_id].model_copy(update={"enabled": False})
        await db.save_user_agent_config(user_id, agent_id, config.model_dump())
    else:
        existing = await db.get_user_agent_config(user_id, agent_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Agent not found")
        await db.delete_user_agent_config(user_id, agent_id)

    effective = await _get_effective_configs(user_id)
    override_rows = await db.get_user_agent_configs(user_id)
    effective_ids = {c.id for c in effective}
    disabled = []
    for row in override_rows:
        parsed = json.loads(row["config_json"])
        if parsed.get("id") not in effective_ids:
            disabled.append(AgentConfig(**parsed))
    return _configs_to_api_response(list(effective) + disabled)


@app.post("/api/agents/reset")
async def reset_agents(request: Request):
    """Reset all agent configs to defaults."""
    user_id = _get_user_id(request)
    await db.delete_all_user_agent_configs(user_id)
    return _configs_to_api_response(get_default_configs())


# --- MCP Server API ---


@app.get("/api/mcp-servers")
async def list_mcp_servers():
    """List all configured MCP servers (including built-in sheerwater)."""
    servers = await db.list_mcp_servers()
    mcp_url = os.environ.get("MCP_SERVER_URL", "http://localhost:8000/sse")
    result = [
        {"id": "sheerwater", "name": "Sheerwater", "url": mcp_url, "transport": "sse", "is_builtin": True},
    ]
    for s in servers:
        d = dict(s)
        d["is_builtin"] = False
        result.append(d)
    return result


@app.post("/api/mcp-servers")
async def add_mcp_server(request: Request):
    """Add a new MCP server."""
    body = await request.json()
    name = body.get("name", "")
    url = body.get("url", "")
    transport = body.get("transport", "sse")

    if not name or not url:
        raise HTTPException(status_code=400, detail="name and url are required")

    server_id = re.sub(r"[^a-zA-Z0-9_]", "_", name.lower())
    if server_id == "sheerwater":
        raise HTTPException(status_code=400, detail="Cannot override the built-in sheerwater server")

    await db.save_mcp_server(server_id, name, url, transport)
    return {"id": server_id, "name": name, "url": url, "transport": transport}


@app.delete("/api/mcp-servers/{server_id}")
async def remove_mcp_server(server_id: str):
    """Remove an MCP server."""
    if server_id == "sheerwater":
        raise HTTPException(status_code=400, detail="Cannot remove the built-in sheerwater server")
    existing = await db.get_mcp_server(server_id)
    if not existing:
        raise HTTPException(status_code=404, detail="MCP server not found")
    await db.delete_mcp_server(server_id)
    return {"status": "deleted"}


# --- Tools API ---


@app.get("/api/tools")
async def list_tools():
    """List all available tools from all configured MCP servers."""
    mcp_server_configs = await db.list_mcp_servers()
    tools = await get_all_mcp_tools(mcp_server_configs)
    return [{"name": t.name, "description": t.description} for t in tools]
