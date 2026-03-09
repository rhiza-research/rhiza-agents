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
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from .agents.tools.mcp import create_mcp_client
from .auth import create_oauth, get_user_from_session, get_user_id, get_user_name
from .config import Config
from .db.sqlite import Database

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global instances
config: Config = None
db: Database = None
agent = None
oauth = None
mcp_tools: list = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global config, db, agent, oauth, mcp_tools

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

    async with AsyncSqliteSaver.from_conn_string(config.checkpoint_db_path) as checkpointer:
        model = ChatAnthropic(model="claude-sonnet-4-20250514", api_key=config.anthropic_api_key)
        agent = create_react_agent(model, mcp_tools, checkpointer=checkpointer)
        logger.info("Agent created")

        yield

    await db.disconnect()


app = FastAPI(title="Rhiza Agents", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY", "dev-secret-key"))

# Templates and static files
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def _extract_text(content) -> str:
    """Extract text from AIMessage content (string or list of content blocks)."""
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
        )
    return (content or "").strip()


def _process_messages(raw_messages):
    """Process raw LangGraph messages into chat messages and activity data.

    Returns (messages, activity_by_turn) where:
    - messages: list of {"type": "human"|"ai", "content": str} for main chat
    - activity_by_turn: list of activity item lists, one per AI turn
    """
    messages = []
    activity_by_turn = []
    current_activity = []

    for msg in raw_messages:
        if isinstance(msg, HumanMessage):
            # Flush any pending activity from previous turn
            if current_activity:
                activity_by_turn.append(current_activity)
                current_activity = []
            messages.append({"type": "human", "content": msg.content})

        elif isinstance(msg, AIMessage):
            text = _extract_text(msg.content)
            tool_calls = msg.tool_calls or []

            if tool_calls:
                # Intermediate message: text is "thinking", tool_calls go to activity
                if text:
                    current_activity.append({"type": "thinking", "content": text})
                for tc in tool_calls:
                    current_activity.append({"type": "tool_call", "name": tc["name"], "args": tc["args"]})
            elif text:
                # Final response for this turn (no tool calls)
                messages.append({"type": "ai", "content": text})
                activity_by_turn.append(current_activity)
                current_activity = []

        elif isinstance(msg, ToolMessage):
            content = msg.content
            try:
                content = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                pass
            current_activity.append({"type": "tool_result", "name": msg.name, "content": content})

    # Flush any remaining activity (turn ended without a final text response)
    if current_activity:
        activity_by_turn.append(current_activity)

    return messages, activity_by_turn


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
    state = await agent.aget_state({"configurable": {"thread_id": conversation_id}})
    raw_messages = state.values.get("messages", [])
    messages, activity_by_turn = _process_messages(raw_messages)

    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "user_name": get_user_name(request),
            "conversations": conversations,
            "current_conversation": conversation,
            "messages": messages,
            "activity_json": json.dumps(activity_by_turn, default=str),
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

    # Invoke the agent
    result = await agent.ainvoke(
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

    messages, activity_by_turn = _process_messages(turn_messages)
    ai_msgs = [m for m in messages if m["type"] == "ai"]
    response_text = ai_msgs[-1]["content"] if ai_msgs else ""
    activity = activity_by_turn[0] if activity_by_turn else []

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
    """Get full message history for a conversation, including tool calls and results."""
    user_id = get_user_id(request)
    conversation = await db.get_conversation(conversation_id, user_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    state = await agent.aget_state({"configurable": {"thread_id": conversation_id}})
    raw_messages = state.values.get("messages", [])

    messages = []
    for msg in raw_messages:
        if isinstance(msg, HumanMessage):
            messages.append({"type": "human", "content": msg.content})
        elif isinstance(msg, AIMessage):
            content = msg.content
            if isinstance(content, list):
                content = "\n".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            entry = {"type": "ai", "content": content or ""}
            if msg.tool_calls:
                entry["tool_calls"] = [{"name": tc["name"], "args": tc["args"]} for tc in msg.tool_calls]
            messages.append(entry)
        elif isinstance(msg, ToolMessage):
            content = msg.content
            # Try to parse JSON tool results for readability
            try:
                content = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                pass
            messages.append({"type": "tool", "name": msg.name, "content": content})

    return {"conversation_id": conversation_id, "messages": messages}


def run():
    """Run the application."""
    import uvicorn

    uvicorn.run("rhiza_agents.main:app", host="0.0.0.0", port=8080, reload=True, reload_dirs=["/app/src"])


if __name__ == "__main__":
    run()
