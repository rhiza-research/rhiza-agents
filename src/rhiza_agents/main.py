"""FastAPI application for rhiza-agents."""

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from .agents.registry import get_default_configs_by_id
from .agents.supervisor import get_agent_graph
from .agents.tools.mcp import create_mcp_client
from .auth import create_oauth, get_user_from_session, get_user_id, get_user_name
from .config import Config
from .db.sqlite import Database

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global instances
config: Config = None
db: Database = None
checkpointer = None
oauth = None
mcp_tools: list = []
_agent_names: dict[str, str] = {}  # agent_id -> display name
_tool_to_agent: dict[str, str] = {}  # tool_name -> agent_id


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global config, db, checkpointer, oauth, mcp_tools, _agent_names, _tool_to_agent

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

    configs_by_id = get_default_configs_by_id()
    _agent_names = {agent_id: c.name for agent_id, c in configs_by_id.items()}

    # Build tool -> agent mapping for agent name tracking
    for agent_id, c in configs_by_id.items():
        if "mcp:sheerwater" in c.tools:
            for t in mcp_tools:
                _tool_to_agent[t.name] = agent_id

    async with AsyncSqliteSaver.from_conn_string(config.checkpoint_db_path) as cp:
        checkpointer = cp
        logger.info("Supervisor graph ready (built on first request)")

        yield

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


def _process_messages(raw_messages):
    """Process raw LangGraph messages into a single ordered list.

    Each item has a "type" field: "human", "ai", "thinking", "tool_call", "tool_result".
    AI responses include "agent_name" when known. Handoff messages are filtered out.
    """
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
                    if agent_id in _agent_names:
                        current_agent = agent_id
                elif tc["name"] in _tool_to_agent:
                    current_agent = _tool_to_agent[tc["name"]]

            agent_name = _agent_names.get(msg.name) or _agent_names.get(current_agent)

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
    graph = await get_agent_graph(mcp_tools, checkpointer)
    state = await graph.aget_state({"configurable": {"thread_id": conversation_id}})
    raw_messages = state.values.get("messages", [])
    all_messages = _process_messages(raw_messages)
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


class SendMessageResponse(BaseModel):
    conversation_id: str
    response: str
    activity: list[dict]
    agent_name: str | None = None


@app.post("/api/chat", response_model=SendMessageResponse)
async def send_chat_message(request: Request, body: SendMessageRequest, user: dict = Depends(require_auth)):
    """Send a message and get an agent response."""
    user_id = get_user_id(request)

    if body.conversation_id:
        conversation = await db.get_conversation(body.conversation_id, user_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        conversation_id = body.conversation_id
    else:
        conversation_id = str(uuid.uuid4())
        await db.create_conversation(conversation_id, user_id)

    # Invoke the supervisor graph
    graph = await get_agent_graph(mcp_tools, checkpointer)
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content=body.message)]},
        config={"configurable": {"thread_id": conversation_id}},
    )

    # Find the new turn's messages (after the last HumanMessage)
    all_msgs = result["messages"]
    turn_start = len(all_msgs)
    for i in range(len(all_msgs) - 1, -1, -1):
        if isinstance(all_msgs[i], HumanMessage):
            turn_start = i
            break
    turn_messages = all_msgs[turn_start:]

    all_messages = _process_messages(turn_messages)
    ai_msgs = [m for m in all_messages if m["type"] == "ai"]
    response_text = ai_msgs[-1]["content"] if ai_msgs else ""
    agent_name = ai_msgs[-1].get("agent_name") if ai_msgs else None
    activity = [m for m in all_messages if m["type"] in ("thinking", "tool_call", "tool_result")]

    # Update conversation title if first message
    conversation = await db.get_conversation(conversation_id, user_id)
    if not conversation.get("title"):
        title = body.message[:50] + ("..." if len(body.message) > 50 else "")
        await db.update_conversation_title(conversation_id, user_id, title)

    await db.touch_conversation(conversation_id)

    return SendMessageResponse(
        conversation_id=conversation_id,
        response=response_text,
        activity=activity,
        agent_name=agent_name,
    )


@app.get("/api/conversations")
async def list_conversations(request: Request, user: dict = Depends(require_auth)):
    """List user's conversations."""
    user_id = get_user_id(request)
    return await db.list_conversations(user_id)


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(request: Request, conversation_id: str, user: dict = Depends(require_auth)):
    """Delete a conversation."""
    user_id = get_user_id(request)
    await db.delete_conversation(conversation_id, user_id)
    return {"status": "deleted"}


@app.get("/api/tools")
async def list_tools(user: dict = Depends(require_auth)):
    """List available MCP tools."""
    return [{"name": t.name, "description": t.description} for t in mcp_tools]


@app.get("/api/conversations/{conversation_id}/messages")
async def get_conversation_messages(request: Request, conversation_id: str, user: dict = Depends(require_auth)):
    """Get full ordered message history for a conversation, for debugging and analysis."""
    user_id = get_user_id(request)
    conversation = await db.get_conversation(conversation_id, user_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    graph = await get_agent_graph(mcp_tools, checkpointer)
    state = await graph.aget_state({"configurable": {"thread_id": conversation_id}})
    raw_messages = state.values.get("messages", [])

    return {"conversation_id": conversation_id, "messages": _process_messages(raw_messages)}


def run():
    """Run the application."""
    import uvicorn

    uvicorn.run("rhiza_agents.main:app", host="0.0.0.0", port=8080, reload=True, reload_dirs=["/app/src"])


if __name__ == "__main__":
    run()
