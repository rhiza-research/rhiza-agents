"""User settings API routes."""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ..deps import get_db, get_user_id, require_auth

router = APIRouter(tags=["settings"])


@router.get("/api/settings")
async def get_settings(request: Request, user: dict = Depends(require_auth)):
    """Get all settings for the current user."""
    db = get_db(request)
    user_id = get_user_id(request)
    settings = await db.get_user_settings(user_id)
    return {"settings": settings}


class UpdateSettingRequest(BaseModel):
    value: str


@router.put("/api/settings/{key}")
async def update_setting(request: Request, key: str, body: UpdateSettingRequest, user: dict = Depends(require_auth)):
    """Set a user setting."""
    db = get_db(request)
    user_id = get_user_id(request)
    await db.set_user_setting(user_id, key, body.value)
    return {"key": key, "value": body.value}
