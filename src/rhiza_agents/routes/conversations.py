"""Conversation list/delete, messages, and files API routes."""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request

from ..agents.registry import get_default_configs, merge_configs
from ..agents.supervisor import get_agent_graph
from ..agents.tools.sandbox import cleanup_sandbox
from ..db.models import AgentConfig
from ..deps import (
    _user_mcp_cache,
    get_checkpointer,
    get_db,
    get_mcp_tools,
    get_mcp_tools_by_server,
    get_user_id,
    get_vectorstore_manager,
    require_auth,
)
from ..messages import build_name_mappings, process_messages

router = APIRouter(tags=["conversations"])


async def _get_mcp_tools_for_owner(request: Request, owner_id: str) -> tuple[dict[str, list], dict[str, str]]:
    """Get MCP tools for a specific user (owner), not necessarily the session user.

    Used for read-only access to other users' conversations where agent name
    resolution needs the owner's MCP config.
    """
    from ..agents.tools.mcp import load_mcp_tools_for_server

    db = get_db(request)
    system_tools = get_mcp_tools_by_server(request)

    result = dict(system_tools)
    names: dict[str, str] = {}
    user_servers = await db.list_mcp_servers(owner_id)
    for server in user_servers:
        sid = server["id"]
        names[sid] = server["name"]
        if server.get("user_id") is None:
            continue
        if not server.get("enabled", True):
            continue
        if sid not in _user_mcp_cache:
            _user_mcp_cache[sid] = await load_mcp_tools_for_server(server["url"], server.get("transport", "sse"))
        if _user_mcp_cache[sid]:
            result[sid] = _user_mcp_cache[sid]
    return result, names


async def _get_effective_configs(request: Request, user_id: str) -> list[AgentConfig]:
    """Get effective agent configs for a user (defaults + overrides, merged)."""
    db = get_db(request)
    defaults = get_default_configs()
    override_rows = await db.get_user_agent_configs(user_id)
    if not override_rows:
        return defaults
    overrides = [json.loads(row["config_json"]) for row in override_rows]
    return merge_configs(defaults, overrides)


@router.get("/api/conversations")
async def list_conversations(request: Request, user: dict = Depends(require_auth)):
    """List user's conversations."""
    db = get_db(request)
    user_id = get_user_id(request)
    return await db.list_conversations(user_id)


@router.delete("/api/conversations/{conversation_id}")
async def delete_conversation(request: Request, conversation_id: str, user: dict = Depends(require_auth)):
    """Delete a conversation and clean up its sandbox."""
    db = get_db(request)
    user_id = get_user_id(request)
    await db.delete_conversation(conversation_id, user_id)
    await asyncio.to_thread(cleanup_sandbox, conversation_id)
    return {"status": "deleted"}


@router.get("/api/conversations/{conversation_id}/messages")
async def get_conversation_messages(request: Request, conversation_id: str, user: dict = Depends(require_auth)):
    """Get full ordered message history for a conversation, for debugging and analysis.

    Supports read-only access to other users' conversations — the conversation
    owner's configs are used for agent name resolution.
    """
    db = get_db(request)
    user_id = get_user_id(request)
    mcp_tools = get_mcp_tools(request)
    checkpointer = get_checkpointer(request)
    vectorstore_manager = get_vectorstore_manager(request)

    conversation = await db.get_conversation(conversation_id, user_id)
    if not conversation:
        # Allow read-only access to any conversation
        conversation = await db.get_conversation_by_id(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

    # Use the conversation owner's configs for agent name resolution
    owner_id = conversation.get("user_id", user_id)
    user_mcp, mcp_names = await _get_mcp_tools_for_owner(request, owner_id)
    graph = await get_agent_graph(
        mcp_tools,
        checkpointer,
        user_id=owner_id,
        db=db,
        vectorstore_manager=vectorstore_manager,
        mcp_tools_by_server=user_mcp,
        mcp_server_names=mcp_names,
    )
    effective = await _get_effective_configs(request, owner_id)
    agent_names, tool_to_agent_map = build_name_mappings(effective, mcp_tools)
    state = await graph.aget_state({"configurable": {"thread_id": conversation_id}})
    raw_messages = state.values.get("messages", [])

    return {
        "conversation_id": conversation_id,
        "messages": process_messages(raw_messages, agent_names),
    }


@router.get("/api/conversations/{conversation_id}/files")
async def list_conversation_files(request: Request, conversation_id: str, user: dict = Depends(require_auth)):
    """List files in a conversation's virtual filesystem."""
    db = get_db(request)
    user_id = get_user_id(request)
    mcp_tools = get_mcp_tools(request)
    checkpointer = get_checkpointer(request)
    vectorstore_manager = get_vectorstore_manager(request)

    conversation = await db.get_conversation(conversation_id, user_id)
    if not conversation:
        # Allow read-only access to other users' conversations
        conversation = await db.get_conversation_by_id(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

    owner_id = conversation.get("user_id", user_id)
    user_mcp, mcp_names = await _get_mcp_tools_for_owner(request, owner_id)
    graph = await get_agent_graph(
        mcp_tools,
        checkpointer,
        user_id=owner_id,
        db=db,
        vectorstore_manager=vectorstore_manager,
        mcp_tools_by_server=user_mcp,
        mcp_server_names=mcp_names,
    )
    state = await graph.aget_state({"configurable": {"thread_id": conversation_id}})
    files = state.values.get("files", {})

    file_list = []
    for path, file_data in files.items():
        content_lines = file_data.get("content", [])
        content_str = "\n".join(content_lines)
        file_list.append(
            {
                "path": path,
                "size": len(content_str.encode("utf-8")),
                "source": file_data.get("source", "agent"),
                "modified_at": file_data.get("modified_at", ""),
            }
        )
    return file_list


@router.get("/api/conversations/{conversation_id}/files/{file_path:path}")
async def get_conversation_file(
    request: Request, conversation_id: str, file_path: str, user: dict = Depends(require_auth)
):
    """Get a file's contents from a conversation's virtual filesystem."""
    db = get_db(request)
    user_id = get_user_id(request)
    mcp_tools = get_mcp_tools(request)
    checkpointer = get_checkpointer(request)
    vectorstore_manager = get_vectorstore_manager(request)

    conversation = await db.get_conversation(conversation_id, user_id)
    if not conversation:
        # Allow read-only access to other users' conversations
        conversation = await db.get_conversation_by_id(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

    owner_id = conversation.get("user_id", user_id)
    user_mcp, mcp_names = await _get_mcp_tools_for_owner(request, owner_id)
    graph = await get_agent_graph(
        mcp_tools,
        checkpointer,
        user_id=owner_id,
        db=db,
        vectorstore_manager=vectorstore_manager,
        mcp_tools_by_server=user_mcp,
        mcp_server_names=mcp_names,
    )
    state = await graph.aget_state({"configurable": {"thread_id": conversation_id}})
    files = state.values.get("files", {})

    # Normalize path -- the stored keys start with /
    lookup_path = file_path if file_path.startswith("/") else "/" + file_path
    file_data = files.get(lookup_path)
    if not file_data:
        raise HTTPException(status_code=404, detail="File not found")

    content_lines = file_data.get("content", [])
    content_str = "\n".join(content_lines)

    return {
        "path": lookup_path,
        "content": content_str,
        "modified_at": file_data.get("modified_at", ""),
    }
