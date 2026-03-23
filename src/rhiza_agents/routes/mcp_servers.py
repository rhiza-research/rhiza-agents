"""MCP server CRUD API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..agents.graph import invalidate_graph_cache
from ..deps import (
    _user_mcp_cache,
    get_db,
    get_mcp_tools_by_server,
    get_user_id,
    invalidate_user_mcp_cache,
    require_auth,
)

router = APIRouter(tags=["mcp-servers"])


@router.get("/api/mcp-servers")
async def list_mcp_servers(request: Request, user: dict = Depends(require_auth)):
    """List all MCP servers visible to the user."""
    db = get_db(request)
    user_id = get_user_id(request)
    system_tools = get_mcp_tools_by_server(request)
    servers = await db.list_mcp_servers(user_id)
    result = []
    for s in servers:
        sid = s["id"]
        tool_count = len(system_tools.get(sid, [])) or len(_user_mcp_cache.get(sid, []))
        result.append(
            {
                "id": sid,
                "name": s["name"],
                "url": s["url"],
                "transport": s.get("transport", "sse"),
                "enabled": bool(s.get("enabled", True)),
                "system": s.get("user_id") is None,
                "tool_count": tool_count,
            }
        )
    return result


class MCPServerCreate(BaseModel):
    name: str
    url: str
    transport: str = "sse"


@router.post("/api/mcp-servers")
async def create_mcp_server(request: Request, body: MCPServerCreate, user: dict = Depends(require_auth)):
    """Add a user MCP server."""
    db = get_db(request)
    user_id = get_user_id(request)
    server_id = f"user-{uuid.uuid4().hex[:12]}"
    server = await db.create_mcp_server(server_id, user_id, body.name, body.url, body.transport)
    invalidate_graph_cache()
    return server


class MCPServerUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    transport: str | None = None
    enabled: bool | None = None


@router.put("/api/mcp-servers/{server_id}")
async def update_mcp_server(
    request: Request, server_id: str, body: MCPServerUpdate, user: dict = Depends(require_auth)
):
    """Update a user MCP server."""
    db = get_db(request)
    server = await db.get_mcp_server(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    if server.get("user_id") is None:
        raise HTTPException(status_code=403, detail="Cannot modify system servers")
    if server.get("user_id") != get_user_id(request):
        raise HTTPException(status_code=403, detail="Not your server")

    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    await db.update_mcp_server(server_id, **fields)
    invalidate_user_mcp_cache(server_id)
    invalidate_graph_cache()
    return {"ok": True}


@router.delete("/api/mcp-servers/{server_id}")
async def delete_mcp_server(request: Request, server_id: str, user: dict = Depends(require_auth)):
    """Delete a user MCP server."""
    db = get_db(request)
    server = await db.get_mcp_server(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    if server.get("user_id") is None:
        raise HTTPException(status_code=403, detail="Cannot delete system servers")
    if server.get("user_id") != get_user_id(request):
        raise HTTPException(status_code=403, detail="Not your server")

    await db.delete_mcp_server(server_id)
    invalidate_user_mcp_cache(server_id)
    invalidate_graph_cache()
    return {"ok": True}


@router.post("/api/mcp-servers/{server_id}/test")
async def test_mcp_server(request: Request, server_id: str, user: dict = Depends(require_auth)):
    """Test connectivity to an MCP server and return its tools."""
    from ..agents.tools.mcp import load_mcp_tools_for_server

    db = get_db(request)
    server = await db.get_mcp_server(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    tools = await load_mcp_tools_for_server(server["url"], server.get("transport", "sse"))
    if tools:
        _user_mcp_cache[server_id] = tools
    return {
        "connected": len(tools) > 0,
        "tool_count": len(tools),
        "tools": [{"name": t.name, "description": t.description} for t in tools],
    }
