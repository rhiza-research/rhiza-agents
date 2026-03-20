"""FastAPI application for rhiza-agents."""

import asyncio
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from .agents.graph import invalidate_graph_cache
from .agents.registry import get_default_configs, get_default_configs_by_id, merge_configs
from .agents.supervisor import get_agent_graph
from .agents.tools.mcp import create_mcp_client
from .agents.tools.sandbox import cleanup_idle_sandboxes, cleanup_sandbox, is_sandbox_available
from .auth import create_oauth, get_user_from_session, get_user_id, get_user_name
from .config import Config
from .db.models import AgentConfig
from .db.sqlite import Database

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global instances
config: Config = None
db: Database = None
checkpointer = None
oauth = None
mcp_tools: list = []
vectorstore_manager = None
_agent_names: dict[str, str] = {}  # agent_id -> display name
_tool_to_agent: dict[str, str] = {}  # tool_name -> agent_id


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global config, db, checkpointer, oauth, mcp_tools, vectorstore_manager, _agent_names, _tool_to_agent

    config = Config.from_env()
    db = Database(config.database_url)
    oauth = create_oauth(config)

    await db.connect()
    logger.info("Connected to database")

    mcp_client = create_mcp_client(config.mcp_server_url)
    for attempt in range(10):
        try:
            mcp_tools = await mcp_client.get_tools()
            break
        except Exception:
            if attempt == 9:
                raise
            logger.info("MCP server not ready, retrying in %ds...", attempt + 1)
            await asyncio.sleep(attempt + 1)
    logger.info("Loaded %d MCP tools", len(mcp_tools))

    # Initialize vector store manager if chroma_persist_dir is set
    if config.chroma_persist_dir:
        from .vectorstore.manager import VectorStoreManager

        vectorstore_manager = VectorStoreManager(config.chroma_persist_dir)
        logger.info("Initialized VectorStoreManager at %s", config.chroma_persist_dir)

    configs_by_id = get_default_configs_by_id()
    _agent_names = {agent_id: c.name for agent_id, c in configs_by_id.items()}

    # Build tool -> agent mapping for agent name tracking
    for agent_id, c in configs_by_id.items():
        if "mcp:sheerwater" in c.tools:
            for t in mcp_tools:
                _tool_to_agent[t.name] = agent_id
        if "sandbox:daytona" in c.tools:
            _tool_to_agent["execute_python_code"] = agent_id

    async def _sandbox_cleanup_loop():
        """Background task to clean up idle sandboxes."""
        while True:
            await asyncio.sleep(60)
            await cleanup_idle_sandboxes()

    async with AsyncSqliteSaver.from_conn_string(config.checkpoint_db_path) as cp:
        checkpointer = cp
        logger.info("Supervisor graph ready (built on first request)")

        cleanup_task = asyncio.create_task(_sandbox_cleanup_loop())
        yield
        cleanup_task.cancel()

    await db.disconnect()


app = FastAPI(title="Rhiza Agents", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY", "dev-secret-key"))

# Templates and static files
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


_THINKING_TAG = "[THINKING]"
_RESPONSE_TAG = "[RESPONSE]"


def _extract_text(content) -> str:
    """Extract text from AIMessage content (string or list of content blocks)."""
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
        )
    return (content or "").strip()


def _classify_text(text: str, has_tool_calls: bool) -> tuple[str, str]:
    """Classify text as thinking or response.

    Priority:
    1. Explicit [THINKING] / [RESPONSE] tags (highest priority)
    2. If the AIMessage also has tool_calls, the text is thinking (intermediate step)
    3. Otherwise, the text is a response

    Returns (phase, content) where phase is "thinking" or "response".
    """
    stripped = text.strip()
    if stripped.startswith(_THINKING_TAG):
        return "thinking", stripped[len(_THINKING_TAG) :].strip()
    if stripped.startswith(_RESPONSE_TAG):
        return "response", stripped[len(_RESPONSE_TAG) :].strip()
    if has_tool_calls:
        return "thinking", text
    return "response", text


_HANDOFF_BACK_KEY = "__is_handoff_back"
_TRANSFER_PREFIX = "transfer_to_"


def _build_name_mappings(configs: list[AgentConfig]) -> tuple[dict[str, str], dict[str, str]]:
    """Build agent_names and tool_to_agent mappings from a config list."""
    agent_names = {c.id: c.name for c in configs}
    tool_to_agent = {}
    for c in configs:
        if "mcp:sheerwater" in c.tools:
            for t in mcp_tools:
                tool_to_agent[t.name] = c.id
        if "sandbox:daytona" in c.tools:
            tool_to_agent["execute_python_code"] = c.id
    return agent_names, tool_to_agent


_SUMMARY_PREFIX = "Summary of earlier conversation:"
_MESSAGE_COUNT_THRESHOLD = 50
_MESSAGES_TO_KEEP = 10


async def _maybe_summarize(graph, conversation_id: str):
    """Summarize and prune checkpoint if message history is too long.

    Runs as a background task after streaming completes. Replaces old
    messages with a concise summary to keep the checkpoint bounded.
    """
    try:
        state = await graph.aget_state({"configurable": {"thread_id": conversation_id}})
        messages = state.values.get("messages", [])

        if len(messages) <= _MESSAGE_COUNT_THRESHOLD:
            return

        to_summarize = messages[:-_MESSAGES_TO_KEEP]
        existing_summary = ""

        # Check if there's already a summary message at the start
        if (
            to_summarize
            and isinstance(to_summarize[0], AIMessage)
            and _extract_text(to_summarize[0].content).startswith(_SUMMARY_PREFIX)
        ):
            existing_summary = to_summarize[0].content
            to_summarize = to_summarize[1:]

        if existing_summary:
            prompt = (
                f"{existing_summary}\n\n"
                "Update this summary to include the following new messages. "
                "Be concise but preserve key facts, decisions, and results:\n\n"
            )
        else:
            prompt = (
                "Summarize the following conversation. Be concise but preserve "
                "key facts, decisions, data results, and any code that was executed. "
                "This summary will replace the original messages:\n\n"
            )

        for msg in to_summarize:
            role = msg.__class__.__name__.replace("Message", "")
            content = _extract_text(msg.content) if hasattr(msg, "content") else ""
            if content:
                prompt += f"{role}: {content[:500]}\n"

        model = ChatAnthropic(model="claude-haiku-3-20240307").with_retry(stop_after_attempt=3)
        summary_response = await model.ainvoke([HumanMessage(content=prompt)])
        summary_text = f"{_SUMMARY_PREFIX}\n{_extract_text(summary_response.content)}"

        # Remove old messages (including existing summary if present)
        updates = [RemoveMessage(id=msg.id) for msg in to_summarize]
        if (
            messages
            and isinstance(messages[0], AIMessage)
            and _extract_text(messages[0].content).startswith(_SUMMARY_PREFIX)
        ):
            updates.append(RemoveMessage(id=messages[0].id))

        await graph.aupdate_state(
            {"configurable": {"thread_id": conversation_id}},
            {"messages": updates},
        )

        # Prepend summary as an AIMessage so it's visible to the LLM
        # (SystemMessage would be stripped by trim_messages include_system=False)
        await graph.aupdate_state(
            {"configurable": {"thread_id": conversation_id}},
            {"messages": [AIMessage(content=summary_text)]},
        )

        logger.info(
            "Summarized conversation %s: %d messages -> %d kept + summary",
            conversation_id,
            len(messages),
            _MESSAGES_TO_KEEP,
        )
    except Exception:
        logger.exception("Failed to summarize conversation %s", conversation_id)


def _process_messages(raw_messages, agent_names=None, tool_to_agent_map=None):
    """Process raw LangGraph messages into a single ordered list.

    Each item has a "type" field: "human", "ai", "thinking", "tool_call", "tool_result".
    AI responses include "agent_name" when known. Handoff messages are filtered out.
    """
    names = agent_names or _agent_names
    t2a = tool_to_agent_map or _tool_to_agent
    messages = []
    current_agent = None  # track which worker agent is active

    for msg in raw_messages:
        if isinstance(msg, HumanMessage):
            current_agent = None
            messages.append({"type": "human", "content": msg.content})

        elif isinstance(msg, AIMessage):
            # Skip handoff-back messages
            if msg.response_metadata.get(_HANDOFF_BACK_KEY, False):
                continue

            text = _extract_text(msg.content)
            tool_calls = msg.tool_calls or []

            # Track current agent from tool calls
            for tc in tool_calls:
                if tc["name"].startswith(_TRANSFER_PREFIX):
                    agent_id = tc["name"][len(_TRANSFER_PREFIX) :]
                    if agent_id in names:
                        current_agent = agent_id
                elif tc["name"] in t2a:
                    current_agent = t2a[tc["name"]]

            agent_name = names.get(msg.name) or names.get(current_agent)

            if text:
                phase, content = _classify_text(text, bool(tool_calls))
                entry = {"type": "ai" if phase == "response" else "thinking", "content": content}
                if agent_name:
                    entry["agent_name"] = agent_name
                messages.append(entry)

            for tc in tool_calls:
                if tc["name"].startswith(("transfer_to_", "transfer_back_to_")):
                    continue
                messages.append({"type": "tool_call", "name": tc["name"], "args": tc["args"]})

        elif isinstance(msg, ToolMessage):
            # Skip handoff tool messages
            if msg.response_metadata.get(_HANDOFF_BACK_KEY, False):
                continue
            content = msg.content
            try:
                content = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                pass
            messages.append({"type": "tool_result", "name": msg.name, "content": content})

    return messages


def require_auth(request: Request):
    """Dependency that requires authentication."""
    user = get_user_from_session(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# --- Auth Routes ---


@app.get("/login")
async def login(request: Request):
    """Redirect to Keycloak login."""
    redirect_uri = f"{config.base_url}/callback"
    return await oauth.keycloak.authorize_redirect(request, redirect_uri)


@app.get("/callback")
async def callback(request: Request):
    """Handle Keycloak callback."""
    token = await oauth.keycloak.authorize_access_token(request)
    user_info = token.get("userinfo")
    if user_info:
        request.session["user"] = dict(user_info)
    return RedirectResponse(url="/")


@app.get("/logout")
async def logout(request: Request):
    """Log out and clear session."""
    request.session.clear()
    return RedirectResponse(url="/")


# --- Page Routes ---


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main page -- chat if logged in, login otherwise."""
    user = get_user_from_session(request)
    if not user:
        return templates.TemplateResponse("login.html", {"request": request})

    user_id = get_user_id(request)
    conversations = await db.list_conversations(user_id)

    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "user_name": get_user_name(request),
            "conversations": conversations,
            "current_conversation": None,
            "messages": [],
            "activity_json": "[]",
        },
    )


@app.get("/c/{conversation_id}", response_class=HTMLResponse)
async def conversation_page(request: Request, conversation_id: str, user: dict = Depends(require_auth)):
    """View a specific conversation."""
    user_id = get_user_id(request)
    conversation = await db.get_conversation(conversation_id, user_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    conversations = await db.list_conversations(user_id)

    # Load messages from LangGraph checkpointer
    graph = await get_agent_graph(
        mcp_tools,
        checkpointer,
        user_id=user_id,
        db=db,
        vectorstore_manager=vectorstore_manager,
    )
    effective = await _get_effective_configs(user_id)
    agent_names, tool_to_agent_map = _build_name_mappings(effective)
    state = await graph.aget_state({"configurable": {"thread_id": conversation_id}})
    raw_messages = state.values.get("messages", [])
    all_messages = _process_messages(raw_messages, agent_names, tool_to_agent_map)
    chat_messages = [m for m in all_messages if m["type"] in ("human", "ai")]
    activity = [m for m in all_messages if m["type"] in ("thinking", "tool_call", "tool_result")]

    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "user_name": get_user_name(request),
            "conversations": conversations,
            "current_conversation": conversation,
            "messages": chat_messages,
            "activity_json": json.dumps(activity, default=str),
        },
    )


# --- API Routes ---


class SendMessageRequest(BaseModel):
    message: str
    conversation_id: str | None = None


@app.post("/api/chat/stream")
async def stream_chat_message(
    request: Request,
    body: SendMessageRequest,
    user: dict = Depends(require_auth),
):
    """Send a message and stream the response via SSE."""
    user_id = get_user_id(request)

    if body.conversation_id:
        conversation = await db.get_conversation(body.conversation_id, user_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        conversation_id = body.conversation_id
    else:
        conversation_id = str(uuid.uuid4())
        await db.create_conversation(conversation_id, user_id)

    graph = await get_agent_graph(
        mcp_tools,
        checkpointer,
        user_id=user_id,
        db=db,
        vectorstore_manager=vectorstore_manager,
    )
    effective = await _get_effective_configs(user_id)
    agent_names, _ = _build_name_mappings(effective)

    async def event_generator():
        yield f"event: conversation_id\ndata: {json.dumps({'conversation_id': conversation_id})}\n\n"

        current_agent = None
        node_buffer = ""
        node_mode = None  # None (undecided), "thinking", or "response"

        try:
            async for event in graph.astream_events(
                {"messages": [HumanMessage(content=body.message)]},
                config={"configurable": {"thread_id": conversation_id}, "recursion_limit": 50},
                version="v2",
            ):
                kind = event["event"]

                if kind == "on_chat_model_stream":
                    chunk = event["data"]["chunk"]
                    text = _extract_text(chunk.content) if hasattr(chunk, "content") else ""
                    if not text:
                        continue

                    node = event.get("metadata", {}).get("langgraph_node", "")
                    display = agent_names.get(node, node)
                    if node != current_agent:
                        # Flush any buffered thinking text to activity panel
                        if node_mode == "thinking" and node_buffer:
                            content = node_buffer.lstrip().removeprefix(_THINKING_TAG).strip()
                            if content:
                                yield f"event: thinking\ndata: {json.dumps({'content': content})}\n\n"
                        current_agent = node
                        node_buffer = ""
                        node_mode = None
                        yield f"event: agent_start\ndata: {json.dumps({'agent': display})}\n\n"

                    if node_mode is None:
                        node_buffer += text
                        stripped = node_buffer.lstrip()
                        if len(stripped) >= len(_THINKING_TAG):
                            if stripped.startswith(_THINKING_TAG):
                                node_mode = "thinking"
                            elif stripped.startswith(_RESPONSE_TAG):
                                node_mode = "response"
                                # Flush buffer minus tag as tokens
                                remainder = stripped[len(_RESPONSE_TAG) :]
                                if remainder:
                                    yield f"event: token\ndata: {json.dumps({'content': remainder})}\n\n"
                            else:
                                node_mode = "response"
                                yield f"event: token\ndata: {json.dumps({'content': node_buffer})}\n\n"
                    elif node_mode == "response":
                        yield f"event: token\ndata: {json.dumps({'content': text})}\n\n"
                    else:
                        # thinking mode: accumulate for activity panel
                        node_buffer += text

                elif kind == "on_chat_model_end":
                    # Flush accumulated thinking text to activity panel
                    if node_mode == "thinking" and node_buffer:
                        content = node_buffer.lstrip().removeprefix(_THINKING_TAG).strip()
                        if content:
                            yield f"event: thinking\ndata: {json.dumps({'content': content})}\n\n"
                        node_buffer = ""
                        node_mode = None

                elif kind == "on_tool_start":
                    tool_name = event.get("name", "")
                    if tool_name.startswith(("transfer_to_", "transfer_back_to_")):
                        continue
                    tool_input = event.get("data", {}).get("input", {})
                    data = json.dumps({"name": tool_name, "input": tool_input}, default=str)
                    yield f"event: tool_start\ndata: {data}\n\n"

                elif kind == "on_tool_end":
                    tool_name = event.get("name", "")
                    if tool_name.startswith(("transfer_to_", "transfer_back_to_")):
                        continue
                    tool_output = event.get("data", {}).get("output", "")
                    if hasattr(tool_output, "content"):
                        tool_output = tool_output.content
                    yield (
                        f"event: tool_end\ndata: "
                        f"{json.dumps({'name': tool_name, 'output': str(tool_output)[:1000]})}\n\n"
                    )

        except Exception as e:
            logger.exception("Streaming error")
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

        # Update conversation metadata
        await db.touch_conversation(conversation_id)
        conv = await db.get_conversation(conversation_id, user_id)
        if conv and not conv.get("title"):
            title = body.message[:50] + ("..." if len(body.message) > 50 else "")
            await db.update_conversation_title(conversation_id, user_id, title)

        # Summarize old messages in the background if the conversation is long
        asyncio.create_task(_maybe_summarize(graph, conversation_id))

        yield f"event: done\ndata: {json.dumps({})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/conversations")
async def list_conversations(request: Request, user: dict = Depends(require_auth)):
    """List user's conversations."""
    user_id = get_user_id(request)
    return await db.list_conversations(user_id)


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(request: Request, conversation_id: str, user: dict = Depends(require_auth)):
    """Delete a conversation and clean up its sandbox."""
    user_id = get_user_id(request)
    await db.delete_conversation(conversation_id, user_id)
    await asyncio.to_thread(cleanup_sandbox, conversation_id)
    return {"status": "deleted"}


@app.get("/api/tools")
async def list_tools(user: dict = Depends(require_auth)):
    """List available MCP tools."""
    return [{"name": t.name, "description": t.description} for t in mcp_tools]


@app.get("/api/tool-types")
async def list_tool_types(user: dict = Depends(require_auth)):
    """List available tool types and their availability status."""
    return [
        {"id": "mcp:sheerwater", "name": "Sheerwater MCP Tools", "available": len(mcp_tools) > 0},
        {"id": "sandbox:daytona", "name": "Code Sandbox (Daytona)", "available": is_sandbox_available()},
    ]


# --- Vector Store API Routes ---


@app.get("/api/vectorstores")
async def list_vectorstores(request: Request, user: dict = Depends(require_auth)):
    """List user's vector stores."""
    user_id = get_user_id(request)
    return await db.list_vectorstores(user_id)


@app.post("/api/vectorstores")
async def create_vectorstore(request: Request, user: dict = Depends(require_auth)):
    """Create a new vector store."""
    if not vectorstore_manager:
        raise HTTPException(status_code=503, detail="Vector store not configured")
    user_id = get_user_id(request)
    body = await request.json()

    display_name = body.get("name", "").strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="Name is required")
    description = body.get("description", "")

    # Namespace collection name per user
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", display_name.lower())
    collection_name = f"{user_id}_{sanitized}"

    vs_id = str(uuid.uuid4())
    vectorstore_manager.create_collection(collection_name, {"description": description})
    record = await db.create_vectorstore(vs_id, user_id, collection_name, display_name, description)
    return record


@app.delete("/api/vectorstores/{vs_id}")
async def delete_vectorstore(request: Request, vs_id: str, user: dict = Depends(require_auth)):
    """Delete a vector store."""
    user_id = get_user_id(request)
    vs = await db.get_vectorstore(vs_id, user_id)
    if not vs:
        raise HTTPException(status_code=404, detail="Vector store not found")

    # Delete ChromaDB collection
    if vectorstore_manager:
        try:
            vectorstore_manager.delete_collection(vs["collection_name"])
        except Exception:
            logger.warning("Failed to delete ChromaDB collection %s", vs["collection_name"], exc_info=True)

    # Delete DB record
    await db.delete_vectorstore(vs_id, user_id)

    # Remove from any agent configs that reference this vectorstore
    override_rows = await db.get_user_agent_configs(user_id)
    for row in override_rows:
        parsed = json.loads(row["config_json"])
        vs_ids = parsed.get("vectorstore_ids", [])
        if vs_id in vs_ids:
            vs_ids.remove(vs_id)
            parsed["vectorstore_ids"] = vs_ids
            await db.save_user_agent_config(user_id, parsed["id"], parsed)

    invalidate_graph_cache()
    return {"status": "deleted"}


@app.post("/api/vectorstores/{vs_id}/upload")
async def upload_documents(
    request: Request,
    vs_id: str,
    files: list[UploadFile] = File(...),
    user: dict = Depends(require_auth),
):
    """Upload documents to a vector store."""
    if not vectorstore_manager:
        raise HTTPException(status_code=503, detail="Vector store not configured")

    user_id = get_user_id(request)
    vs = await db.get_vectorstore(vs_id, user_id)
    if not vs:
        raise HTTPException(status_code=404, detail="Vector store not found")

    from .vectorstore.manager import chunk_text, extract_text_from_file

    total_chunks = 0
    for file in files:
        content = await file.read()
        try:
            text = extract_text_from_file(file.filename, content)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        chunks = chunk_text(text)
        if chunks:
            metadatas = [{"source": file.filename, "chunk_index": i} for i in range(len(chunks))]
            vectorstore_manager.add_documents(vs["collection_name"], chunks, metadatas)
            total_chunks += len(chunks)

    new_count = vs["document_count"] + total_chunks
    await db.update_vectorstore_doc_count(vs_id, new_count)

    return {"document_count": new_count, "chunks_added": total_chunks}


@app.get("/api/conversations/{conversation_id}/messages")
async def get_conversation_messages(request: Request, conversation_id: str, user: dict = Depends(require_auth)):
    """Get full ordered message history for a conversation, for debugging and analysis."""
    user_id = get_user_id(request)
    conversation = await db.get_conversation(conversation_id, user_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    graph = await get_agent_graph(
        mcp_tools,
        checkpointer,
        user_id=user_id,
        db=db,
        vectorstore_manager=vectorstore_manager,
    )
    effective = await _get_effective_configs(user_id)
    agent_names, tool_to_agent_map = _build_name_mappings(effective)
    state = await graph.aget_state({"configurable": {"thread_id": conversation_id}})
    raw_messages = state.values.get("messages", [])

    return {
        "conversation_id": conversation_id,
        "messages": _process_messages(raw_messages, agent_names, tool_to_agent_map),
    }


async def _get_effective_configs(user_id: str) -> list[AgentConfig]:
    """Get effective agent configs for a user (defaults + overrides, merged)."""
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


_AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


# --- Config Page Route ---


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request, user: dict = Depends(require_auth)):
    """Config editor page."""
    return templates.TemplateResponse(
        "config_editor.html",
        {"request": request, "user_name": get_user_name(request)},
    )


# --- Agent Config API Routes ---


@app.get("/api/agents")
async def get_agents(request: Request, user: dict = Depends(require_auth)):
    """Get effective agent configs for the current user."""
    user_id = get_user_id(request)
    effective = await _get_effective_configs(user_id)
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


@app.put("/api/agents/{agent_id}")
async def update_agent(request: Request, agent_id: str, user: dict = Depends(require_auth)):
    """Update an agent config override."""
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

    effective = await _get_effective_configs(user_id)
    override_rows = await db.get_user_agent_configs(user_id)
    effective_ids = {c.id for c in effective}
    disabled = []
    for row in override_rows:
        parsed = json.loads(row["config_json"])
        if parsed.get("id") not in effective_ids:
            disabled.append(AgentConfig(**parsed))
    return _configs_to_api_response(list(effective) + disabled)


@app.post("/api/agents")
async def create_agent(request: Request, user: dict = Depends(require_auth)):
    """Create a new custom agent."""
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

    effective = await _get_effective_configs(user_id)
    return _configs_to_api_response(effective)


@app.delete("/api/agents/{agent_id}")
async def delete_agent(request: Request, agent_id: str, user: dict = Depends(require_auth)):
    """Disable a default agent or delete a custom agent."""
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
async def reset_agents(request: Request, user: dict = Depends(require_auth)):
    """Reset all agent configs to defaults."""
    user_id = get_user_id(request)
    await db.delete_all_user_agent_configs(user_id)
    invalidate_graph_cache()
    return _configs_to_api_response(get_default_configs())


def run():
    """Run the application."""
    import uvicorn

    uvicorn.run("rhiza_agents.main:app", host="0.0.0.0", port=8080, reload=True, reload_dirs=["/app/src"])


if __name__ == "__main__":
    run()
