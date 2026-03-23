"""Vector store CRUD API routes."""

import json
import logging
import re
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from ..agents.graph import invalidate_graph_cache
from ..deps import get_db, get_user_id, get_vectorstore_manager, require_auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["vectorstores"])


@router.get("/api/vectorstores")
async def list_vectorstores(request: Request, user: dict = Depends(require_auth)):
    """List user's vector stores."""
    db = get_db(request)
    user_id = get_user_id(request)
    return await db.list_vectorstores(user_id)


@router.post("/api/vectorstores")
async def create_vectorstore(request: Request, user: dict = Depends(require_auth)):
    """Create a new vector store."""
    vsm = get_vectorstore_manager(request)
    if not vsm:
        raise HTTPException(status_code=503, detail="Vector store not configured")
    db = get_db(request)
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
    vsm.create_collection(collection_name, {"description": description})
    record = await db.create_vectorstore(vs_id, user_id, collection_name, display_name, description)
    return record


@router.delete("/api/vectorstores/{vs_id}")
async def delete_vectorstore(request: Request, vs_id: str, user: dict = Depends(require_auth)):
    """Delete a vector store."""
    db = get_db(request)
    vsm = get_vectorstore_manager(request)
    user_id = get_user_id(request)
    vs = await db.get_vectorstore(vs_id, user_id)
    if not vs:
        raise HTTPException(status_code=404, detail="Vector store not found")

    if vsm:
        try:
            vsm.delete_collection(vs["collection_name"])
        except Exception:
            logger.warning("Failed to delete ChromaDB collection %s", vs["collection_name"], exc_info=True)

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


@router.post("/api/vectorstores/{vs_id}/upload")
async def upload_documents(
    request: Request,
    vs_id: str,
    files: list[UploadFile] = File(...),
    user: dict = Depends(require_auth),
):
    """Upload documents to a vector store."""
    vsm = get_vectorstore_manager(request)
    if not vsm:
        raise HTTPException(status_code=503, detail="Vector store not configured")

    db = get_db(request)
    user_id = get_user_id(request)
    vs = await db.get_vectorstore(vs_id, user_id)
    if not vs:
        raise HTTPException(status_code=404, detail="Vector store not found")

    from ..vectorstore.manager import chunk_text, extract_text_from_file

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
            vsm.add_documents(vs["collection_name"], chunks, metadatas)
            total_chunks += len(chunks)

    new_count = vs["document_count"] + total_chunks
    await db.update_vectorstore_doc_count(vs_id, new_count)

    return {"document_count": new_count, "chunks_added": total_chunks}
