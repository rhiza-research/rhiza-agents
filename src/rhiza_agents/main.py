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
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
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
            _tool_to_agent["write_file"] = agent_id
            _tool_to_agent["run_file"] = agent_id

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


def _extract_content_blocks(content) -> tuple[str, str]:
    """Extract text and reasoning from AIMessage content.

    Returns (text, reasoning) where each is a concatenation of the
    respective content blocks. If content is a plain string, it's
    returned as text with empty reasoning.
    """
    if isinstance(content, list):
        text_parts = []
        reasoning_parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "reasoning":
                    reasoning_parts.append(block.get("reasoning", ""))
            elif hasattr(block, "type"):
                if block.type == "text":
                    text_parts.append(getattr(block, "text", ""))
                elif block.type == "reasoning":
                    text_val = getattr(block, "reasoning", "")
                    reasoning_parts.append(text_val)
        return "\n".join(text_parts), "\n".join(reasoning_parts)
    return (content or "").strip(), ""


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
            tool_to_agent["write_file"] = c.id
            tool_to_agent["run_file"] = c.id
    return agent_names, tool_to_agent


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

            text, reasoning = _extract_content_blocks(msg.content)
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

            if reasoning:
                messages.append({"type": "thinking", "content": reasoning})

            if text:
                entry = {"type": "ai", "content": text}
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
            "has_files": False,
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
    has_files = bool(state.values.get("files"))

    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "user_name": get_user_name(request),
            "conversations": conversations,
            "current_conversation": conversation,
            "messages": chat_messages,
            "activity_json": json.dumps(activity, default=str),
            "has_files": has_files,
        },
    )


# --- API Routes ---


class SendMessageRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    execution_mode: str = "auto"  # "auto" or "review"


class ResumeRequest(BaseModel):
    conversation_id: str
    decision: str = "approve"  # "approve" or "reject"
    message: str | None = None  # rejection reason


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
        stream_input = {"messages": [HumanMessage(content=body.message)]}

        try:
            while True:
                auto_resume = False
                async for chunk in graph.astream(
                    stream_input,
                    config={"configurable": {"thread_id": conversation_id}},
                    stream_mode=["messages", "updates", "custom"],
                    version="v2",
                    subgraphs=True,
                ):
                    chunk_type = chunk["type"]

                    if chunk_type == "messages":
                        token, metadata = chunk["data"]
                        if not hasattr(token, "content"):
                            continue

                        text, reasoning = _extract_content_blocks(token.content)

                        node = metadata.get("lc_agent_name") or metadata.get("langgraph_node", "")
                        display = agent_names.get(node, node)
                        if node != current_agent:
                            current_agent = node
                            yield f"event: agent_start\ndata: {json.dumps({'agent': display})}\n\n"

                        if reasoning:
                            yield f"event: thinking\ndata: {json.dumps({'content': reasoning})}\n\n"
                        if text:
                            yield f"event: token\ndata: {json.dumps({'content': text})}\n\n"

                    elif chunk_type == "updates":
                        update_data = chunk["data"]

                        # HITL interrupts appear as __interrupt__ in updates
                        if "__interrupt__" in update_data:
                            if body.execution_mode == "auto":
                                # Auto-approve: resume immediately without user interaction
                                stream_input = Command(resume={"decisions": [{"type": "approve"}]})
                                auto_resume = True
                                break
                            else:
                                for intr in update_data["__interrupt__"]:
                                    yield (f"event: interrupt\ndata: {json.dumps(intr, default=str)}\n\n")
                                continue

                        # Extract tool call/result info from node updates
                        for _node_name, node_data in update_data.items():
                            if not isinstance(node_data, dict):
                                continue
                            for msg in node_data.get("messages", []):
                                # Tool calls from AI messages
                                if hasattr(msg, "tool_calls") and msg.tool_calls:
                                    for tc in msg.tool_calls:
                                        if tc["name"].startswith(("transfer_to_", "transfer_back_to_")):
                                            continue
                                        data = json.dumps(
                                            {"name": tc["name"], "args": tc["args"]},
                                            default=str,
                                        )
                                        yield f"event: tool_start\ndata: {data}\n\n"
                                # Tool results from ToolMessages
                                if isinstance(msg, ToolMessage):
                                    if msg.name and msg.name.startswith(("transfer_to_", "transfer_back_to_")):
                                        continue
                                    tool_content = msg.content
                                    try:
                                        tool_content = json.loads(tool_content)
                                    except (json.JSONDecodeError, TypeError):
                                        pass
                                    yield (
                                        f"event: tool_end\ndata: "
                                        f"{json.dumps({'name': msg.name, 'output': str(tool_content)[:1000]})}\n\n"
                                    )

                    elif chunk_type == "custom":
                        custom_data = chunk["data"]
                        if isinstance(custom_data, dict) and custom_data.get("type") == "files_changed":
                            yield f"event: files_changed\ndata: {json.dumps({})}\n\n"

                if not auto_resume:
                    break

        except Exception as e:
            logger.exception("Streaming error")
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

        # Update conversation metadata
        await db.touch_conversation(conversation_id)
        conv = await db.get_conversation(conversation_id, user_id)
        if conv and not conv.get("title"):
            title = body.message[:50] + ("..." if len(body.message) > 50 else "")
            await db.update_conversation_title(conversation_id, user_id, title)

        yield f"event: files_changed\ndata: {json.dumps({})}\n\n"
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


@app.post("/api/chat/resume")
async def resume_chat(
    request: Request,
    body: ResumeRequest,
    user: dict = Depends(require_auth),
):
    """Resume an interrupted graph execution (HITL approve/reject)."""
    user_id = get_user_id(request)
    conversation = await db.get_conversation(body.conversation_id, user_id)
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
    agent_names, _ = _build_name_mappings(effective)

    if body.decision == "approve":
        decision = {"type": "approve"}
    else:
        decision = {"type": "reject", "message": body.message or "Rejected by user"}

    async def event_generator():
        current_agent = None

        try:
            async for chunk in graph.astream(
                Command(resume={"decisions": [decision]}),
                config={"configurable": {"thread_id": body.conversation_id}},
                stream_mode=["messages", "updates", "custom"],
                version="v2",
                subgraphs=True,
            ):
                chunk_type = chunk["type"]

                if chunk_type == "messages":
                    token, metadata = chunk["data"]
                    if not hasattr(token, "content"):
                        continue

                    text, reasoning = _extract_content_blocks(token.content)

                    node = metadata.get("lc_agent_name") or metadata.get("langgraph_node", "")
                    display = agent_names.get(node, node)
                    if node != current_agent:
                        current_agent = node
                        yield f"event: agent_start\ndata: {json.dumps({'agent': display})}\n\n"

                    if reasoning:
                        yield f"event: thinking\ndata: {json.dumps({'content': reasoning})}\n\n"
                    if text:
                        yield f"event: token\ndata: {json.dumps({'content': text})}\n\n"

                elif chunk_type == "updates":
                    update_data = chunk["data"]

                    if "__interrupt__" in update_data:
                        for interrupt in update_data["__interrupt__"]:
                            yield f"event: interrupt\ndata: {json.dumps(interrupt, default=str)}\n\n"
                        continue

                    for _node_name, node_data in update_data.items():
                        if not isinstance(node_data, dict):
                            continue
                        for msg in node_data.get("messages", []):
                            if hasattr(msg, "tool_calls") and msg.tool_calls:
                                for tc in msg.tool_calls:
                                    if tc["name"].startswith(("transfer_to_", "transfer_back_to_")):
                                        continue
                                    data = json.dumps(
                                        {"name": tc["name"], "args": tc["args"]},
                                        default=str,
                                    )
                                    yield f"event: tool_start\ndata: {data}\n\n"
                            if isinstance(msg, ToolMessage):
                                if msg.name and msg.name.startswith(("transfer_to_", "transfer_back_to_")):
                                    continue
                                tool_content = msg.content
                                try:
                                    tool_content = json.loads(tool_content)
                                except (json.JSONDecodeError, TypeError):
                                    pass
                                yield (
                                    f"event: tool_end\ndata: "
                                    f"{json.dumps({'name': msg.name, 'output': str(tool_content)[:1000]})}\n\n"
                                )

                elif chunk_type == "custom":
                    custom_data = chunk["data"]
                    if isinstance(custom_data, dict) and custom_data.get("type") == "files_changed":
                        yield f"event: files_changed\ndata: {json.dumps({})}\n\n"

        except Exception as e:
            logger.exception("Resume streaming error")
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

        yield f"event: files_changed\ndata: {json.dumps({})}\n\n"
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


@app.get("/api/conversations/{conversation_id}/files")
async def list_conversation_files(request: Request, conversation_id: str, user: dict = Depends(require_auth)):
    """List files in a conversation's virtual filesystem."""
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
                "modified_at": file_data.get("modified_at", ""),
            }
        )
    return file_list


@app.get("/api/conversations/{conversation_id}/files/{file_path:path}")
async def get_conversation_file(
    request: Request, conversation_id: str, file_path: str, user: dict = Depends(require_auth)
):
    """Get a file's contents from a conversation's virtual filesystem."""
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
