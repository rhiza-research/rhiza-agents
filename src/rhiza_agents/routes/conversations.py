"""Conversation list/delete, messages, and files API routes."""

import asyncio
import base64
import json
import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from ..agents.registry import get_default_configs, merge_configs
from ..agents.supervisor import get_agent_graph
from ..agents.tools.files import fetch_file_content, list_thread_files
from ..agents.tools.sandbox import cleanup_sandbox, cleanup_thread_workspace_async
from ..db.models import AgentConfig
from ..deps import (
    _skill_cache,
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

logger = logging.getLogger(__name__)

router = APIRouter(tags=["conversations"])


def _content_disposition_attachment(filename: str) -> str:
    """Build a Content-Disposition header value safe for any filename.

    Per RFC 6266 + RFC 5987: emit ``filename="<ascii>"`` for old clients
    plus ``filename*=UTF-8''<pct-encoded>`` for the modern unicode form.
    Control characters (CR/LF in particular) are stripped before either
    form is produced so the result cannot break out of the header into
    a new header — closing CVE-style injection where a filename like
    ``foo"; X-Injected: bar.txt`` would otherwise add a header.

    Filenames are sourced from sandbox paths controlled either by a
    skill's output or by the agent itself; this helper makes both
    sources safe to render verbatim in the response header.
    """
    # Drop ASCII control characters (\x00-\x1F, \x7F). These are what
    # actually let an attacker inject a new header line.
    safe = "".join(c for c in filename if 32 <= ord(c) < 127 or ord(c) > 127)
    if not safe:
        safe = "file"
    # ASCII-only fallback for ancient clients. Non-ASCII characters
    # become "?" (replace error handler), then any remaining quotes /
    # backslashes get escaped per RFC 6266 grammar.
    ascii_only = safe.encode("ascii", errors="replace").decode("ascii")
    ascii_escaped = ascii_only.replace("\\", "\\\\").replace('"', '\\"')
    # RFC 5987 percent-encoded form preserves the original unicode.
    pct_encoded = quote(safe, safe="")
    return f"attachment; filename=\"{ascii_escaped}\"; filename*=UTF-8''{pct_encoded}"


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


async def _get_skill_tools_for_owner(request: Request, owner_id: str) -> dict:
    """Get skill tools for a specific user (owner), not necessarily the session user."""
    from ..agents.tools.skills import create_skill_tool

    db = get_db(request)
    skills = await db.list_skills(owner_id)
    result = {}
    for skill in skills:
        if not skill.get("enabled", True):
            continue
        sid = skill["id"]
        if sid not in _skill_cache:
            try:
                _skill_cache[sid] = create_skill_tool(skill)
            except (ValueError, Exception):
                continue
        result[sid] = _skill_cache[sid]
    return result


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
    """Delete a conversation and clean up its sandbox + workspace.

    Only the conversation owner can delete. Non-owner requests get 404
    (do not leak existence beyond what the read-only access allows) /
    403 (when ownership mismatches a known conversation). The owner check
    happens before any side effects so a non-owner request cannot disrupt
    the owner's running sandbox.
    """
    db = get_db(request)
    user_id = get_user_id(request)

    conversation = await db.get_conversation_by_id(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conversation.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="You do not own this conversation")

    # DB row first so a partial failure leaves no orphan; sandbox/volume
    # cleanup is best-effort and logged on failure. Workspace cleanup
    # runs BEFORE the sandbox kill so cleanup_thread_workspace can reuse
    # the active sandbox to do the rm — saves a ~5–15s sandbox-create.
    # If the sandbox is already gone (idle-cleaned), the helper spins up
    # a temp sandbox to perform the cleanup.
    await db.delete_conversation(conversation_id, user_id)
    await cleanup_thread_workspace_async(conversation_id)
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
    owner_skills = await _get_skill_tools_for_owner(request, owner_id)
    graph = await get_agent_graph(
        mcp_tools,
        checkpointer,
        user_id=owner_id,
        db=db,
        vectorstore_manager=vectorstore_manager,
        mcp_tools_by_server=user_mcp,
        mcp_server_names=mcp_names,
        skill_tools=owner_skills,
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
async def list_conversation_files(
    request: Request,
    conversation_id: str,
    scope: str = "session",
    user: dict = Depends(require_auth),
):
    """List files relevant to a conversation.

    Two scopes:
      - ``scope=session`` (default): files tracked in the conversation's
        accumulated state — every file the agent wrote in this thread,
        plus any new outputs from past tool runs. Works without an active
        sandbox; rendered from the LangGraph checkpoint.
      - ``scope=workspace``: the live contents of /workspace on the
        sandbox volume. Lazy-creates the sandbox if none is running.
        Use this view to see everything currently on the volume,
        including files this thread didn't track in state.

    Read-only viewers (shared link without ownership) can use both scopes.
    """
    db = get_db(request)
    user_id = get_user_id(request)
    mcp_tools = get_mcp_tools(request)
    checkpointer = get_checkpointer(request)
    vectorstore_manager = get_vectorstore_manager(request)

    conversation = await db.get_conversation(conversation_id, user_id)
    if not conversation:
        conversation = await db.get_conversation_by_id(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

    owner_id = conversation.get("user_id", user_id)

    if scope == "workspace":
        # Live filesystem listing — triggers sandbox creation if needed.
        try:
            return await list_thread_files(conversation_id)
        except Exception as e:
            logger.warning("Failed to list workspace files for %s", conversation_id, exc_info=True)
            raise HTTPException(status_code=502, detail=f"Failed to list workspace files: {e}") from e

    # Default: session-scope listing from state metadata.
    user_mcp, mcp_names = await _get_mcp_tools_for_owner(request, owner_id)
    owner_skills = await _get_skill_tools_for_owner(request, owner_id)
    graph = await get_agent_graph(
        mcp_tools,
        checkpointer,
        user_id=owner_id,
        db=db,
        vectorstore_manager=vectorstore_manager,
        mcp_tools_by_server=user_mcp,
        mcp_server_names=mcp_names,
        skill_tools=owner_skills,
    )
    state = await graph.aget_state({"configurable": {"thread_id": conversation_id}})
    files = state.values.get("files", {})

    file_list = []
    for path, file_data in files.items():
        # Schema after refactor: {size, modified_at, source, ...} — no content.
        # Tolerate legacy entries (which had {content: [...], ...}) by computing
        # size from content_lines if size key is absent.
        size = file_data.get("size")
        if size is None:
            content_lines = file_data.get("content", []) or []
            encoding = file_data.get("encoding")
            if encoding == "base64":
                b64_str = content_lines[0] if content_lines else ""
                size = len(b64_str) * 3 // 4
            else:
                size = len("\n".join(content_lines).encode("utf-8"))
        file_list.append(
            {
                "path": path,
                "size": size,
                "source": file_data.get("source", "agent"),
                "modified_at": file_data.get("modified_at", ""),
            }
        )
    return file_list


@router.get("/api/conversations/{conversation_id}/files/{file_path:path}")
async def get_conversation_file(
    request: Request, conversation_id: str, file_path: str, user: dict = Depends(require_auth)
):
    """Get a file's contents from the conversation's workspace.

    Fetched live from the sandbox filesystem. Lazy-creates the sandbox if
    none exists; the cold-start latency is on the click that triggered
    this fetch. Read-only viewers can read any path the workspace exposes.

    Returns ``{path, content, encoding, modified_at}``. Binary content is
    returned base64-encoded (encoding="base64"); text is utf-8 decoded
    with ``errors='replace'`` (encoding="utf-8").
    """
    db = get_db(request)
    user_id = get_user_id(request)
    mcp_tools = get_mcp_tools(request)
    checkpointer = get_checkpointer(request)
    vectorstore_manager = get_vectorstore_manager(request)

    conversation = await db.get_conversation(conversation_id, user_id)
    if not conversation:
        conversation = await db.get_conversation_by_id(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

    lookup_path = file_path if file_path.startswith("/") else "/" + file_path

    # If state has legacy content for this path (pre-volume migration),
    # pass it along as a fallback so fetch_file_content can lazy-write it
    # to the workspace volume on first read.
    owner_id = conversation.get("user_id", user_id)
    legacy_fallback: bytes | None = None
    try:
        user_mcp, mcp_names = await _get_mcp_tools_for_owner(request, owner_id)
        owner_skills = await _get_skill_tools_for_owner(request, owner_id)
        graph = await get_agent_graph(
            mcp_tools,
            checkpointer,
            user_id=owner_id,
            db=db,
            vectorstore_manager=vectorstore_manager,
            mcp_tools_by_server=user_mcp,
            mcp_server_names=mcp_names,
            skill_tools=owner_skills,
        )
        state = await graph.aget_state({"configurable": {"thread_id": conversation_id}})
        files = state.values.get("files", {})
        entry = files.get(lookup_path)
        if entry and "content" in entry:
            content_lines = entry.get("content", []) or []
            encoding = entry.get("encoding")
            if encoding == "base64":
                b64_str = content_lines[0] if content_lines else ""
                try:
                    legacy_fallback = base64.b64decode(b64_str)
                except Exception:
                    legacy_fallback = None
            else:
                legacy_fallback = "\n".join(content_lines).encode("utf-8")
    except Exception:
        # Migration is best-effort; if we can't read state, just try the
        # live filesystem and let it 404 if missing.
        logger.warning("Legacy fallback lookup failed for %s/%s", conversation_id, lookup_path, exc_info=True)

    try:
        content_bytes, modified_at = await fetch_file_content(conversation_id, lookup_path, legacy_fallback)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail="File not found") from e
    except Exception as e:
        logger.warning("Failed to fetch file %s for conversation %s", lookup_path, conversation_id, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Failed to fetch file: {e}") from e

    # Heuristic: try utf-8 first (errors=strict to detect binary), fall back
    # to base64. This avoids encoding plain text files as base64.
    try:
        text = content_bytes.decode("utf-8")
        return {
            "path": lookup_path,
            "content": text,
            "encoding": "utf-8",
            "modified_at": modified_at,
        }
    except UnicodeDecodeError:
        return {
            "path": lookup_path,
            "content": base64.b64encode(content_bytes).decode("ascii"),
            "encoding": "base64",
            "modified_at": modified_at,
        }


@router.get("/api/conversations/{conversation_id}/files/{file_path:path}/raw")
async def download_conversation_file(
    request: Request, conversation_id: str, file_path: str, user: dict = Depends(require_auth)
):
    """Download a file's raw bytes for the conversation.

    Same auth as the JSON-content endpoint (read-only access via shared
    link is allowed). Streams binary content with the appropriate filename.
    """
    db = get_db(request)
    user_id = get_user_id(request)

    conversation = await db.get_conversation(conversation_id, user_id)
    if not conversation:
        conversation = await db.get_conversation_by_id(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

    lookup_path = file_path if file_path.startswith("/") else "/" + file_path

    try:
        content_bytes, _ = await fetch_file_content(conversation_id, lookup_path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail="File not found") from e
    except Exception as e:
        logger.warning("Failed to fetch file %s for conversation %s", lookup_path, conversation_id, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Failed to fetch file: {e}") from e

    filename = lookup_path.rsplit("/", 1)[-1] or "file"
    return Response(
        content=content_bytes,
        media_type="application/octet-stream",
        headers={"Content-Disposition": _content_disposition_attachment(filename)},
    )
