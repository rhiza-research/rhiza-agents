"""Chat streaming API routes: POST /api/chat/stream and POST /api/chat/resume."""

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel

from ..agents.registry import get_default_configs, merge_configs
from ..agents.supervisor import get_agent_graph
from ..agents.tools.sandbox import _collect_referenced_names
from ..agents.turn import run_agent_turn
from ..db.models import AgentConfig
from ..deps import (
    get_checkpointer,
    get_db,
    get_mcp_tools,
    get_mcp_tools_for_user,
    get_skill_tools_for_user,
    get_user_id,
    get_user_name,
    get_vectorstore_manager,
    is_chat_logging_enabled,
    require_auth,
)
from ..logging_config import chat_event_logger
from ..observability import (
    get_langfuse_client,
    sync_user_prompts,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


class SendMessageRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    execution_mode: str = "auto"  # "auto" or "review"


class ResumeRequest(BaseModel):
    conversation_id: str
    decision: str = "approve"  # "approve" or "reject"
    message: str | None = None  # rejection reason


class FeedbackRequest(BaseModel):
    trace_id: str
    value: int  # +1 for thumbs up, -1 for thumbs down
    comment: str | None = None


async def _get_effective_configs(request: Request, user_id: str) -> list[AgentConfig]:
    """Get effective agent configs for a user (defaults + overrides, merged)."""
    db = get_db(request)
    defaults = get_default_configs()
    override_rows = await db.get_user_agent_configs(user_id)
    if not override_rows:
        return defaults
    overrides = [json.loads(row["config_json"]) for row in override_rows]
    return merge_configs(defaults, overrides)


async def _enrich_interrupt_payload(intr_data, db, user_id: str) -> dict:
    """Add ``missing_credentials`` to an interrupt payload before streaming it.

    Walks every ``action_requests[*].args.credentials`` materialization plan
    (via the canonical flattener in sandbox) and compares the referenced
    secret names against the user's credential store. Each action_request
    gets its own ``missing_credentials`` list, and a top-level aggregate is
    attached so the frontend can render a single warning without re-walking.

    Returns a plain dict (the normalized shape — the original ``intr_data``
    may be a Pydantic model or langgraph ``Interrupt``). The frontend uses
    this purely as a warning ("these names won't be injected because you
    haven't set them") and does not gate the Approve button on it. Missing
    names are silently dropped from the materialization at execution time,
    and the underlying CLI/script is responsible for surfacing a clear error
    if it actually needed a credential that wasn't injected.
    """
    # Normalize to a plain dict. HumanInTheLoopMiddleware passes us a dict
    # already but subclasses might not.
    if hasattr(intr_data, "model_dump"):
        payload = intr_data.model_dump()
    elif isinstance(intr_data, dict):
        payload = dict(intr_data)
    else:
        # Fall back to whatever json.dumps(default=str) would have produced —
        # no credential checks possible in that case.
        return {"value": str(intr_data), "missing_credentials": []}

    try:
        stored_names = set(await db.list_credential_names(user_id))
    except Exception:
        logger.warning("Failed to load credential names for interrupt enrichment", exc_info=True)
        stored_names = set()

    all_missing: list[str] = []
    seen_missing: set[str] = set()
    for ar in payload.get("action_requests") or []:
        if not isinstance(ar, dict):
            continue
        args = ar.get("args") if isinstance(ar.get("args"), dict) else {}
        creds = args.get("credentials") if isinstance(args.get("credentials"), list) else []
        referenced = _collect_referenced_names(creds) if creds else []
        ar_missing = [n for n in referenced if n not in stored_names]
        ar["missing_credentials"] = ar_missing
        for n in ar_missing:
            if n not in seen_missing:
                seen_missing.add(n)
                all_missing.append(n)

    payload["missing_credentials"] = all_missing
    return payload


@router.post("/api/chat/stream")
async def stream_chat_message(
    request: Request,
    body: SendMessageRequest,
    user: dict = Depends(require_auth),
):
    """Send a message and stream the response via SSE."""
    db = get_db(request)
    user_id = get_user_id(request)
    mcp_tools = get_mcp_tools(request)
    checkpointer = get_checkpointer(request)
    vectorstore_manager = get_vectorstore_manager(request)

    if body.conversation_id:
        conversation = await db.get_conversation(body.conversation_id, user_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        conversation_id = body.conversation_id
    else:
        conversation_id = str(uuid.uuid4())
        await db.create_conversation(conversation_id, user_id)

    log_chat_events = await is_chat_logging_enabled(request, user_id)

    def _log_event(event: str, **data):
        if log_chat_events:
            chat_event_logger.info(event, extra={"conversation_id": conversation_id, "user_id": user_id, **data})

    _log_event("graph_build", status="start")
    user_mcp, mcp_names = await get_mcp_tools_for_user(request)
    user_skills = await get_skill_tools_for_user(request)
    # Compute effective configs first so we can sync prompts before the graph
    # is built and bind the agent's prompt object to its model.
    effective = await _get_effective_configs(request, user_id)
    prompt_refs, prompt_objects = sync_user_prompts(get_user_name(request), effective)
    graph = await get_agent_graph(
        mcp_tools,
        checkpointer,
        user_configs=effective,
        db=db,
        vectorstore_manager=vectorstore_manager,
        mcp_tools_by_server=user_mcp,
        mcp_server_names=mcp_names,
        skill_tools=user_skills,
    )
    _log_event(
        "graph_build",
        status="ready",
        mcp_servers={mcp_names.get(k, k): len(v) for k, v in user_mcp.items()},
    )

    async def event_generator():
        yield f"event: conversation_id\ndata: {json.dumps({'conversation_id': conversation_id})}\n\n"

        accumulated_text = []

        def _flush_accumulated():
            nonlocal accumulated_text
            if accumulated_text:
                _log_event(
                    "agent_message",
                    content="".join(accumulated_text)[:2000],
                )
                accumulated_text = []

        _log_event("user_message", content=body.message[:500])

        try:
            async for ev in run_agent_turn(
                graph,
                thread_id=conversation_id,
                stream_input={"messages": [HumanMessage(content=body.message)]},
                execution_mode=body.execution_mode,
                prompt_refs=prompt_refs,
                prompt_objects=prompt_objects,
                user_id=user_id,
            ):
                ev_type = ev["type"]
                if ev_type == "trace_id":
                    yield f"event: trace_id\ndata: {json.dumps({'trace_id': ev['trace_id']})}\n\n"
                elif ev_type == "thinking":
                    yield f"event: thinking\ndata: {json.dumps({'content': ev['content']})}\n\n"
                elif ev_type == "token":
                    yield f"event: token\ndata: {json.dumps({'content': ev['content']})}\n\n"
                    accumulated_text.append(ev["content"])
                elif ev_type == "tool_start":
                    data = json.dumps({"name": ev["name"], "args": ev["args"]}, default=str)
                    yield f"event: tool_start\ndata: {data}\n\n"
                    _log_event("tool_start", tool=ev["name"], tool_args=str(ev["args"])[:500])
                elif ev_type == "tool_end":
                    yield (f"event: tool_end\ndata: {json.dumps({'name': ev['name'], 'output': ev['output']})}\n\n")
                    _log_event("tool_end", tool=ev["name"], output=ev["output"])
                elif ev_type == "chart":
                    yield f"event: chart\ndata: {json.dumps({'url': ev['url']})}\n\n"
                elif ev_type == "files_changed":
                    yield f"event: files_changed\ndata: {json.dumps({})}\n\n"
                elif ev_type == "interrupt":
                    enriched = await _enrich_interrupt_payload(ev["value"], db, user_id)
                    yield f"event: interrupt\ndata: {json.dumps(enriched, default=str)}\n\n"
                    _log_event("interrupt", data=str(enriched)[:500])

        except Exception as e:
            logger.exception("Streaming error")
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
            _log_event("error", error=str(e))

        _flush_accumulated()

        # Update conversation metadata
        await db.touch_conversation(conversation_id)
        conv = await db.get_conversation(conversation_id, user_id)
        if conv and not conv.get("title"):
            title = body.message[:50] + ("..." if len(body.message) > 50 else "")
            await db.update_conversation_title(conversation_id, user_id, title)

        yield f"event: files_changed\ndata: {json.dumps({})}\n\n"
        yield f"event: done\ndata: {json.dumps({})}\n\n"
        _log_event("done")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/chat/resume")
async def resume_chat(
    request: Request,
    body: ResumeRequest,
    user: dict = Depends(require_auth),
):
    """Resume an interrupted graph execution (HITL approve/reject)."""
    db = get_db(request)
    user_id = get_user_id(request)
    mcp_tools = get_mcp_tools(request)
    checkpointer = get_checkpointer(request)
    vectorstore_manager = get_vectorstore_manager(request)

    conversation = await db.get_conversation(body.conversation_id, user_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    user_mcp, mcp_names = await get_mcp_tools_for_user(request)
    user_skills = await get_skill_tools_for_user(request)
    effective = await _get_effective_configs(request, user_id)
    prompt_refs, prompt_objects = sync_user_prompts(get_user_name(request), effective)
    graph = await get_agent_graph(
        mcp_tools,
        checkpointer,
        user_configs=effective,
        db=db,
        vectorstore_manager=vectorstore_manager,
        mcp_tools_by_server=user_mcp,
        mcp_server_names=mcp_names,
        skill_tools=user_skills,
    )

    if body.decision == "approve":
        decision = {"type": "approve"}
    else:
        decision = {"type": "reject", "message": body.message or "Rejected by user"}

    log_chat_events = await is_chat_logging_enabled(request, user_id)
    conversation_id = body.conversation_id

    def _log_event(event: str, **data):
        if log_chat_events:
            chat_event_logger.info(event, extra={"conversation_id": conversation_id, "user_id": user_id, **data})

    async def event_generator():
        accumulated_text = []

        def _flush_accumulated():
            nonlocal accumulated_text
            if accumulated_text:
                _log_event(
                    "agent_message",
                    content="".join(accumulated_text)[:2000],
                )
                accumulated_text = []

        _log_event("resume", decision=body.decision)

        try:
            # Resume streams a single round to completion. "review" mode means a
            # further interrupt surfaces to the user rather than auto-resuming.
            async for ev in run_agent_turn(
                graph,
                thread_id=body.conversation_id,
                stream_input=Command(resume={"decisions": [decision]}),
                execution_mode="review",
                prompt_refs=prompt_refs,
                prompt_objects=prompt_objects,
                user_id=user_id,
            ):
                ev_type = ev["type"]
                if ev_type == "trace_id":
                    yield f"event: trace_id\ndata: {json.dumps({'trace_id': ev['trace_id']})}\n\n"
                elif ev_type == "thinking":
                    yield f"event: thinking\ndata: {json.dumps({'content': ev['content']})}\n\n"
                elif ev_type == "token":
                    yield f"event: token\ndata: {json.dumps({'content': ev['content']})}\n\n"
                    accumulated_text.append(ev["content"])
                elif ev_type == "tool_start":
                    data = json.dumps({"name": ev["name"], "args": ev["args"]}, default=str)
                    yield f"event: tool_start\ndata: {data}\n\n"
                    _log_event("tool_start", tool=ev["name"], tool_args=str(ev["args"])[:500])
                elif ev_type == "tool_end":
                    yield (f"event: tool_end\ndata: {json.dumps({'name': ev['name'], 'output': ev['output']})}\n\n")
                    _log_event("tool_end", tool=ev["name"], output=ev["output"])
                elif ev_type == "chart":
                    yield f"event: chart\ndata: {json.dumps({'url': ev['url']})}\n\n"
                elif ev_type == "files_changed":
                    yield f"event: files_changed\ndata: {json.dumps({})}\n\n"
                elif ev_type == "interrupt":
                    enriched = await _enrich_interrupt_payload(ev["value"], db, user_id)
                    yield f"event: interrupt\ndata: {json.dumps(enriched, default=str)}\n\n"
                    _log_event("interrupt", data=str(enriched)[:500])

        except Exception as e:
            logger.exception("Resume streaming error")
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
            _log_event("error", error=str(e))

        _flush_accumulated()

        yield f"event: files_changed\ndata: {json.dumps({})}\n\n"
        yield f"event: done\ndata: {json.dumps({})}\n\n"
        _log_event("done")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/chat/feedback")
async def submit_feedback(
    body: FeedbackRequest,
    user: dict = Depends(require_auth),
):
    """Attach a thumbs up/down score to a Langfuse trace.

    The trace id was generated server-side at the start of the chat stream
    that produced the message and surfaced to the client via the `trace_id`
    SSE event. The client posts it back here when the user clicks the thumbs.
    """
    if body.value not in (1, -1):
        raise HTTPException(status_code=400, detail="value must be 1 or -1")

    client = get_langfuse_client()
    if client is None:
        raise HTTPException(status_code=503, detail="Langfuse not configured")

    try:
        client.create_score(
            name="user_feedback",
            value=body.value,
            data_type="NUMERIC",
            trace_id=body.trace_id,
            comment=body.comment,
        )
    except Exception as e:
        logger.exception("Failed to submit Langfuse feedback")
        raise HTTPException(status_code=502, detail=f"Langfuse error: {e}") from e

    return {"ok": True}
