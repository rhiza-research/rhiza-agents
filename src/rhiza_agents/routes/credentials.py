"""Credential CRUD API.

The contract: API responses NEVER include the encrypted ciphertext or the
plaintext value. Only metadata (id, name, timestamps) is exposed. Editing
a credential means submitting a fresh value — there is no "view" or
"reveal" affordance, and the form should always treat the stored value
as opaque.

When ``CREDENTIAL_ENCRYPTION_KEY`` is unset, every route returns 503 so
the feature fails closed.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..agents.graph import invalidate_graph_cache
from ..credentials import credentials_enabled, encrypt_value
from ..deps import get_db, get_user_id, require_auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["credentials"])


def _credential_view(row: dict) -> dict:
    """Project a credential row into the safe public response shape."""
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _require_enabled() -> None:
    if not credentials_enabled():
        raise HTTPException(
            status_code=503,
            detail="Credentials feature is disabled (CREDENTIAL_ENCRYPTION_KEY not set)",
        )


@router.get("/api/credentials")
async def list_credentials(request: Request, user: dict = Depends(require_auth)):
    """List all credentials owned by the user. No secret material is returned."""
    _require_enabled()
    db = get_db(request)
    user_id = get_user_id(request)
    rows = await db.list_credentials(user_id)
    return [_credential_view(r) for r in rows]


class CredentialCreate(BaseModel):
    name: str
    value: str


@router.post("/api/credentials")
async def create_credential(request: Request, body: CredentialCreate, user: dict = Depends(require_auth)):
    _require_enabled()
    db = get_db(request)
    user_id = get_user_id(request)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not body.value:
        raise HTTPException(status_code=400, detail="value must not be empty")

    # Reject duplicate names up front for a clean error message; the DB
    # uniqueness constraint backs this up.
    existing = await db.list_credential_names(user_id)
    if name in existing:
        raise HTTPException(status_code=409, detail=f"a credential named {name!r} already exists")

    ciphertext = encrypt_value(body.value)
    credential_id = f"cred-{uuid.uuid4().hex[:12]}"
    await db.create_credential(
        credential_id=credential_id,
        user_id=user_id,
        name=name,
        value_ciphertext=ciphertext,
    )
    # Worker prompts list available credential names, so adding one must
    # invalidate the graph cache.
    invalidate_graph_cache()
    row = await db.get_credential_meta(credential_id, user_id)
    return _credential_view(row)


class CredentialUpdate(BaseModel):
    name: str | None = None
    # Submitting a value replaces the stored secret. Omit it to leave the
    # secret untouched (e.g. when only renaming).
    value: str | None = None


@router.put("/api/credentials/{credential_id}")
async def update_credential(
    request: Request,
    credential_id: str,
    body: CredentialUpdate,
    user: dict = Depends(require_auth),
):
    _require_enabled()
    db = get_db(request)
    user_id = get_user_id(request)
    existing = await db.get_credential_meta(credential_id, user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Credential not found")

    new_name: str | None = None
    if body.name is not None:
        new_name = body.name.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="name must not be empty")
        if new_name != existing["name"]:
            other_names = await db.list_credential_names(user_id)
            if new_name in other_names:
                raise HTTPException(status_code=409, detail=f"a credential named {new_name!r} already exists")

    ciphertext: bytes | None = None
    if body.value is not None:
        if not body.value:
            raise HTTPException(status_code=400, detail="value must not be empty")
        ciphertext = encrypt_value(body.value)

    await db.update_credential(
        credential_id=credential_id,
        user_id=user_id,
        name=new_name,
        value_ciphertext=ciphertext,
    )
    if new_name is not None:
        invalidate_graph_cache()
    row = await db.get_credential_meta(credential_id, user_id)
    return _credential_view(row)


@router.delete("/api/credentials/{credential_id}")
async def delete_credential(request: Request, credential_id: str, user: dict = Depends(require_auth)):
    _require_enabled()
    db = get_db(request)
    user_id = get_user_id(request)
    existing = await db.get_credential_meta(credential_id, user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Credential not found")
    await db.delete_credential(credential_id, user_id)
    invalidate_graph_cache()
    return {"ok": True}
