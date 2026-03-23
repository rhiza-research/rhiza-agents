"""HTML page routes: /, /c/{id}, /config, /login, /callback, /logout."""

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..deps import get_db, get_user_id, get_user_name, require_auth

router = APIRouter(tags=["pages"])


def _get_templates(request: Request):
    """Get Jinja2Templates from app state."""
    return request.app.state.templates


def _get_static_version(request: Request) -> str:
    """Get static asset cache-busting version from app state."""
    return request.app.state.static_version


# --- Auth Routes ---


@router.get("/login")
async def login(request: Request):
    """Redirect to Keycloak login."""
    config = request.app.state.config
    oauth = request.app.state.oauth
    redirect_uri = f"{config.base_url}/callback"
    return await oauth.keycloak.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def callback(request: Request):
    """Handle Keycloak callback."""
    oauth = request.app.state.oauth
    token = await oauth.keycloak.authorize_access_token(request)
    user_info = token.get("userinfo")
    if user_info:
        request.session["user"] = dict(user_info)
    return RedirectResponse(url="/")


@router.get("/logout")
async def logout(request: Request):
    """Log out and clear session."""
    request.session.clear()
    return RedirectResponse(url="/")


# --- Page Routes ---


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main page -- chat if logged in, login otherwise."""
    templates = _get_templates(request)
    user = request.session.get("user")
    if not user:
        return templates.TemplateResponse("login.html", {"request": request})

    db = get_db(request)
    user_id = get_user_id(request)
    conversations = await db.list_conversations(user_id)

    app_data = {
        "conversations": [{"id": c["id"], "title": c.get("title")} for c in conversations],
        "conversationId": "",
        "userName": get_user_name(request),
    }

    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "user_name": get_user_name(request),
            "app_data_json": json.dumps(app_data),
            "static_version": _get_static_version(request),
        },
    )


@router.get("/c/{conversation_id}", response_class=HTMLResponse)
async def conversation_page(request: Request, conversation_id: str, user: dict = Depends(require_auth)):
    """View a specific conversation."""
    templates = _get_templates(request)
    db = get_db(request)
    user_id = get_user_id(request)
    conversation = await db.get_conversation(conversation_id, user_id)
    if not conversation:
        # Allow read-only access to other users' conversations
        conversation = await db.get_conversation_by_id(conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

    conversations = await db.list_conversations(user_id)

    app_data = {
        "conversations": [{"id": c["id"], "title": c.get("title")} for c in conversations],
        "conversationId": conversation_id,
        "userName": get_user_name(request),
    }

    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "user_name": get_user_name(request),
            "app_data_json": json.dumps(app_data),
            "static_version": _get_static_version(request),
        },
    )
