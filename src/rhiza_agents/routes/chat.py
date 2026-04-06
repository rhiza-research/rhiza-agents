"""Chat streaming API routes: POST /api/chat/stream and POST /api/chat/resume."""

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel

from ..agents.registry import get_default_configs, merge_configs
from ..agents.supervisor import get_agent_graph
from ..db.models import AgentConfig
from ..deps import (
    get_checkpointer,
    get_db,
    get_mcp_tools,
    get_mcp_tools_for_user,
    get_skill_tools_for_user,
    get_user_id,
    get_vectorstore_manager,
    is_chat_logging_enabled,
    require_auth,
)
from ..logging_config import chat_event_logger
from ..messages import (
    build_name_mappings,
    extract_chart_url,
    extract_content_blocks,
    extract_content_blocks_from_token,
    resolve_agent_name,
)
from ..observability import get_langfuse_client, make_langfuse_handler, new_trace_id

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
    graph = await get_agent_graph(
        mcp_tools,
        checkpointer,
        user_id=user_id,
        db=db,
        vectorstore_manager=vectorstore_manager,
        mcp_tools_by_server=user_mcp,
        mcp_server_names=mcp_names,
        skill_tools=user_skills,
    )
    effective = await _get_effective_configs(request, user_id)
    agent_names, tool_to_agent = build_name_mappings(effective, mcp_tools)
    # Map the langgraph default node name "agent" to the supervisor's display name
    supervisor_name = next((c.name for c in effective if c.type == "supervisor"), "Supervisor")
    agent_names["agent"] = supervisor_name
    _log_event(
        "graph_build",
        status="ready",
        agents=", ".join(dict.fromkeys(agent_names.values())),
        mcp_servers={mcp_names.get(k, k): len(v) for k, v in user_mcp.items()},
    )

    def _resolve_tool_agent(tool_name: str, fallback: str | None) -> str | None:
        """Resolve agent display name from tool name via tool_to_agent mapping."""
        agent_id = tool_to_agent.get(tool_name)
        if agent_id:
            return agent_names.get(agent_id, fallback)
        return fallback

    async def event_generator():
        yield f"event: conversation_id\ndata: {json.dumps({'conversation_id': conversation_id})}\n\n"

        current_agent = None
        current_agent_display = None
        accumulated_text = []
        seen_tool_call_ids = set()
        seen_tool_result_ids = set()
        stream_input = {"messages": [HumanMessage(content=body.message)]}

        def _flush_accumulated():
            nonlocal accumulated_text
            if accumulated_text:
                _log_event(
                    "agent_message",
                    agent=current_agent_display,
                    content="".join(accumulated_text)[:2000],
                )
                accumulated_text = []

        _log_event("user_message", content=body.message[:500])

        try:
            while True:
                auto_resume = False
                trace_id = new_trace_id()
                stream_config = {
                    "configurable": {"thread_id": conversation_id},
                    "metadata": {
                        "langfuse_user_id": user_id,
                        "langfuse_session_id": conversation_id,
                    },
                }
                lf_handler = make_langfuse_handler(trace_id=trace_id)
                if lf_handler:
                    stream_config["callbacks"] = [lf_handler]
                    yield f"event: trace_id\ndata: {json.dumps({'trace_id': trace_id})}\n\n"
                async for chunk in graph.astream(
                    stream_input,
                    config=stream_config,
                    stream_mode=["messages", "updates", "custom"],
                    version="v2",
                    subgraphs=True,
                ):
                    chunk_type = chunk["type"]

                    if chunk_type == "messages":
                        token, metadata = chunk["data"]
                        # Only process AI model output, not tool results
                        if isinstance(token, ToolMessage):
                            continue
                        node_name = metadata.get("langgraph_node", "")
                        if node_name == "tools":
                            continue
                        text, reasoning = extract_content_blocks_from_token(token)
                        if not text and not reasoning:
                            continue

                        node = metadata.get("lc_agent_name") or metadata.get("langgraph_node", "")
                        ns = chunk.get("ns", [])
                        display = resolve_agent_name(
                            agent_names,
                            node_name=node,
                            ns=ns,
                            fallback=current_agent_display,
                        )
                        # Find the agent_id for tracking (reverse lookup)
                        agent_id = next((k for k, v in agent_names.items() if v == display), current_agent)
                        if agent_id and agent_id != current_agent:
                            _flush_accumulated()
                            current_agent = agent_id
                            current_agent_display = display
                            yield f"event: agent_start\ndata: {json.dumps({'agent': display})}\n\n"
                            _log_event("agent_start", agent=display)

                        if reasoning:
                            yield f"event: thinking\ndata: {json.dumps({'content': reasoning})}\n\n"
                        if text:
                            yield f"event: token\ndata: {json.dumps({'content': text})}\n\n"
                            accumulated_text.append(text)

                    elif chunk_type == "updates":
                        update_data = chunk["data"]

                        # HITL interrupts appear as __interrupt__ in updates.
                        # Only handle top-level (empty ns) to avoid duplicates
                        # from subgraphs.
                        if "__interrupt__" in update_data:
                            if chunk.get("ns"):
                                continue
                            if body.execution_mode == "auto":
                                # Auto-approve: resume immediately without user interaction
                                stream_input = Command(resume={"decisions": [{"type": "approve"}]})
                                auto_resume = True
                                break
                            else:
                                for intr in update_data["__interrupt__"]:
                                    intr_data = getattr(intr, "value", intr)
                                    yield f"event: interrupt\ndata: {json.dumps(intr_data, default=str)}\n\n"
                                    _log_event("interrupt", data=str(intr_data)[:500])
                                continue

                        # Extract tool call/result info from node updates.
                        # Deduplicate by tool call ID since subgraphs=True
                        # surfaces the same event from both subgraph and parent.
                        ns = chunk.get("ns", [])
                        update_agent_display = resolve_agent_name(agent_names, ns=ns, fallback=current_agent_display)

                        for _node_name, node_data in update_data.items():
                            if not isinstance(node_data, dict):
                                continue
                            for msg in node_data.get("messages", []):
                                # Tool calls from AI messages
                                if hasattr(msg, "tool_calls") and msg.tool_calls:
                                    for tc in msg.tool_calls:
                                        if tc["name"].startswith(("transfer_to_", "transfer_back_to_")):
                                            continue
                                        tc_id = tc.get("id")
                                        if tc_id:
                                            if tc_id in seen_tool_call_ids:
                                                continue
                                            seen_tool_call_ids.add(tc_id)
                                        data = json.dumps(
                                            {"name": tc["name"], "args": tc["args"]},
                                            default=str,
                                        )
                                        yield f"event: tool_start\ndata: {data}\n\n"
                                        tool_agent = _resolve_tool_agent(tc["name"], update_agent_display)
                                        _log_event(
                                            "tool_start",
                                            agent=tool_agent,
                                            tool=tc["name"],
                                            tool_args=str(tc["args"])[:500],
                                        )
                                        # Emit file data immediately so the UI can
                                        # show the file before the checkpoint saves
                                        if tc["name"] == "write_file" and isinstance(tc.get("args"), dict):
                                            file_data = {
                                                "path": tc["args"].get("path", ""),
                                                "content": tc["args"].get("content", ""),
                                            }
                                            yield f"event: file_written\ndata: {json.dumps(file_data)}\n\n"
                                # Tool results from ToolMessages
                                if isinstance(msg, ToolMessage):
                                    if msg.name and msg.name.startswith(("transfer_to_", "transfer_back_to_")):
                                        continue
                                    result_id = getattr(msg, "tool_call_id", None)
                                    if result_id:
                                        if result_id in seen_tool_result_ids:
                                            continue
                                        seen_tool_result_ids.add(result_id)
                                    tool_content = msg.content
                                    # Extract text from content block lists
                                    if isinstance(tool_content, list):
                                        text, _ = extract_content_blocks(tool_content)
                                        tool_content = text or tool_content
                                    if isinstance(tool_content, str):
                                        try:
                                            tool_content = json.loads(tool_content)
                                        except (json.JSONDecodeError, TypeError):
                                            pass
                                    tool_output_str = str(tool_content)[:1000]
                                    yield (
                                        f"event: tool_end\ndata: "
                                        f"{json.dumps({'name': msg.name, 'output': tool_output_str})}\n\n"
                                    )
                                    _log_event(
                                        "tool_end",
                                        agent=_resolve_tool_agent(msg.name, update_agent_display),
                                        tool=msg.name,
                                        output=tool_output_str,
                                    )
                                    # Emit chart event for plotly renders
                                    if msg.name in (
                                        "tool_render_plotly",
                                        "tool_generate_comparison_chart",
                                    ):
                                        html_url = extract_chart_url(msg.content)
                                        if html_url:
                                            yield f"event: chart\ndata: {json.dumps({'url': html_url})}\n\n"
                                    # Emit files_changed for run_file results
                                    # (write_file uses file_written from tool_start instead,
                                    # since files_changed triggers loadFiles which overwrites
                                    # the immediate file display before checkpoint saves)
                                    if msg.name == "run_file":
                                        yield f"event: files_changed\ndata: {json.dumps({})}\n\n"

                    elif chunk_type == "custom":
                        custom_data = chunk["data"]
                        if isinstance(custom_data, dict) and custom_data.get("type") == "files_changed":
                            yield f"event: files_changed\ndata: {json.dumps({})}\n\n"

                if not auto_resume:
                    break

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
    graph = await get_agent_graph(
        mcp_tools,
        checkpointer,
        user_id=user_id,
        db=db,
        vectorstore_manager=vectorstore_manager,
        mcp_tools_by_server=user_mcp,
        mcp_server_names=mcp_names,
        skill_tools=user_skills,
    )
    effective = await _get_effective_configs(request, user_id)
    agent_names, tool_to_agent = build_name_mappings(effective, mcp_tools)
    supervisor_name = next((c.name for c in effective if c.type == "supervisor"), "Supervisor")
    agent_names["agent"] = supervisor_name

    def _resolve_tool_agent(tool_name: str, fallback: str | None) -> str | None:
        agent_id = tool_to_agent.get(tool_name)
        if agent_id:
            return agent_names.get(agent_id, fallback)
        return fallback

    if body.decision == "approve":
        decision = {"type": "approve"}
    else:
        decision = {"type": "reject", "message": body.message or "Rejected by user"}

    log_chat_events = await is_chat_logging_enabled(request, user_id)
    conversation_id = body.conversation_id

    def _log_event(event: str, **data):
        if log_chat_events:
            chat_event_logger.info(event, extra={"conversation_id": conversation_id, "user_id": user_id, **data})

    # Seed initial agent from the interrupted tool so resume logs
    # attribute the first message correctly (before any agent_start fires).
    state = await graph.aget_state(config={"configurable": {"thread_id": conversation_id}})
    initial_agent = None
    initial_agent_display = None
    if state and state.next:
        # state.tasks contains the interrupted tool info
        for task in getattr(state, "tasks", []):
            for intr in getattr(task, "interrupts", []):
                intr_value = getattr(intr, "value", {})
                for ar in intr_value.get("action_requests", []):
                    tool_name = ar.get("name")
                    if tool_name:
                        agent_id = tool_to_agent.get(tool_name)
                        if agent_id and agent_id in agent_names:
                            initial_agent = agent_id
                            initial_agent_display = agent_names[agent_id]
                            break

    async def event_generator():
        current_agent = initial_agent
        current_agent_display = initial_agent_display
        accumulated_text = []
        seen_tool_call_ids = set()
        seen_tool_result_ids = set()

        def _flush_accumulated():
            nonlocal accumulated_text
            if accumulated_text:
                _log_event(
                    "agent_message",
                    agent=current_agent_display,
                    content="".join(accumulated_text)[:2000],
                )
                accumulated_text = []

        _log_event("resume", decision=body.decision)

        try:
            trace_id = new_trace_id()
            resume_config = {
                "configurable": {"thread_id": body.conversation_id},
                "metadata": {
                    "langfuse_user_id": user_id,
                    "langfuse_session_id": body.conversation_id,
                },
            }
            lf_handler = make_langfuse_handler(trace_id=trace_id)
            if lf_handler:
                resume_config["callbacks"] = [lf_handler]
                yield f"event: trace_id\ndata: {json.dumps({'trace_id': trace_id})}\n\n"
            async for chunk in graph.astream(
                Command(resume={"decisions": [decision]}),
                config=resume_config,
                stream_mode=["messages", "updates", "custom"],
                version="v2",
                subgraphs=True,
            ):
                chunk_type = chunk["type"]

                if chunk_type == "messages":
                    token, metadata = chunk["data"]
                    if isinstance(token, ToolMessage):
                        continue
                    if metadata.get("langgraph_node", "") == "tools":
                        continue
                    text, reasoning = extract_content_blocks_from_token(token)
                    if not text and not reasoning:
                        continue

                    node = metadata.get("lc_agent_name") or metadata.get("langgraph_node", "")
                    ns = chunk.get("ns", [])
                    display = resolve_agent_name(agent_names, node_name=node, ns=ns, fallback=current_agent_display)
                    agent_id = next((k for k, v in agent_names.items() if v == display), current_agent)
                    if agent_id and agent_id != current_agent:
                        _flush_accumulated()
                        current_agent = agent_id
                        current_agent_display = display
                        yield f"event: agent_start\ndata: {json.dumps({'agent': display})}\n\n"
                        _log_event("agent_start", agent=display)

                    if reasoning:
                        yield f"event: thinking\ndata: {json.dumps({'content': reasoning})}\n\n"
                    if text:
                        yield f"event: token\ndata: {json.dumps({'content': text})}\n\n"
                        accumulated_text.append(text)

                elif chunk_type == "updates":
                    update_data = chunk["data"]

                    # Interrupts: only from top-level to avoid duplicates
                    if "__interrupt__" in update_data:
                        if chunk.get("ns"):
                            continue
                        for intr in update_data["__interrupt__"]:
                            intr_data = getattr(intr, "value", intr)
                            yield f"event: interrupt\ndata: {json.dumps(intr_data, default=str)}\n\n"
                            _log_event("interrupt", data=str(intr_data)[:500])
                        continue

                    # Extract tool call/result info from node updates.
                    # Deduplicate by tool call ID since subgraphs=True
                    # surfaces the same event from both subgraph and parent.
                    ns = chunk.get("ns", [])
                    update_agent_display = resolve_agent_name(agent_names, ns=ns, fallback=current_agent_display)

                    for _node_name, node_data in update_data.items():
                        if not isinstance(node_data, dict):
                            continue
                        for msg in node_data.get("messages", []):
                            if hasattr(msg, "tool_calls") and msg.tool_calls:
                                for tc in msg.tool_calls:
                                    if tc["name"].startswith(("transfer_to_", "transfer_back_to_")):
                                        continue
                                    tc_id = tc.get("id")
                                    if tc_id:
                                        if tc_id in seen_tool_call_ids:
                                            continue
                                        seen_tool_call_ids.add(tc_id)
                                    data = json.dumps(
                                        {"name": tc["name"], "args": tc["args"]},
                                        default=str,
                                    )
                                    yield f"event: tool_start\ndata: {data}\n\n"
                                    _log_event(
                                        "tool_start",
                                        agent=_resolve_tool_agent(tc["name"], update_agent_display),
                                        tool=tc["name"],
                                        tool_args=str(tc["args"])[:500],
                                    )
                                    # Emit file data immediately so the UI can
                                    # show the file before the checkpoint saves
                                    if tc["name"] == "write_file" and isinstance(tc.get("args"), dict):
                                        file_data = {
                                            "path": tc["args"].get("path", ""),
                                            "content": tc["args"].get("content", ""),
                                        }
                                        yield f"event: file_written\ndata: {json.dumps(file_data)}\n\n"
                            if isinstance(msg, ToolMessage):
                                if msg.name and msg.name.startswith(("transfer_to_", "transfer_back_to_")):
                                    continue
                                result_id = getattr(msg, "tool_call_id", None)
                                if result_id:
                                    if result_id in seen_tool_result_ids:
                                        continue
                                    seen_tool_result_ids.add(result_id)
                                tool_content = msg.content
                                # Extract text from content block lists
                                if isinstance(tool_content, list):
                                    text, _ = extract_content_blocks(tool_content)
                                    tool_content = text or tool_content
                                if isinstance(tool_content, str):
                                    try:
                                        tool_content = json.loads(tool_content)
                                    except (json.JSONDecodeError, TypeError):
                                        pass
                                tool_output_str = str(tool_content)[:1000]
                                yield (
                                    f"event: tool_end\ndata: "
                                    f"{json.dumps({'name': msg.name, 'output': tool_output_str})}\n\n"
                                )
                                _log_event(
                                    "tool_end",
                                    agent=_resolve_tool_agent(msg.name, update_agent_display),
                                    tool=msg.name,
                                    output=tool_output_str,
                                )
                                # Emit chart event for plotly renders
                                if msg.name in (
                                    "tool_render_plotly",
                                    "tool_generate_comparison_chart",
                                ):
                                    html_url = extract_chart_url(msg.content)
                                    if html_url:
                                        yield f"event: chart\ndata: {json.dumps({'url': html_url})}\n\n"
                                if msg.name == "run_file":
                                    yield f"event: files_changed\ndata: {json.dumps({})}\n\n"

                elif chunk_type == "custom":
                    custom_data = chunk["data"]
                    if isinstance(custom_data, dict) and custom_data.get("type") == "files_changed":
                        yield f"event: files_changed\ndata: {json.dumps({})}\n\n"

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
