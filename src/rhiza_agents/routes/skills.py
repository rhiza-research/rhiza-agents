"""Skills CRUD and GitHub install API routes."""

import json
import logging
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..agents.graph import invalidate_graph_cache
from ..agents.tools.skills import parse_skill_md, requires_sandbox
from ..deps import get_db, get_user_id, invalidate_skill_cache, require_auth

logger = logging.getLogger(__name__)

router = APIRouter(tags=["skills"])


@router.get("/api/skills")
async def list_skills(request: Request, user: dict = Depends(require_auth)):
    """List all skills visible to the user (system + user-owned)."""
    db = get_db(request)
    user_id = get_user_id(request)
    skills = await db.list_skills(user_id)
    return [
        {
            "id": s["id"],
            "name": s["name"],
            "description": s["description"],
            "source": s["source"],
            "source_ref": s.get("source_ref"),
            "enabled": bool(s.get("enabled", True)),
            "system": s.get("user_id") is None,
            "requires_sandbox": requires_sandbox(s),
        }
        for s in skills
    ]


@router.get("/api/skills/{skill_id}")
async def get_skill(request: Request, skill_id: str, user: dict = Depends(require_auth)):
    """Get full skill details."""
    db = get_db(request)
    skill = await db.get_skill(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {
        "id": skill["id"],
        "name": skill["name"],
        "description": skill["description"],
        "source": skill["source"],
        "source_ref": skill.get("source_ref"),
        "enabled": bool(skill.get("enabled", True)),
        "system": skill.get("user_id") is None,
        "skill_md": skill["skill_md"],
        "requires_sandbox": requires_sandbox(skill),
        "scripts": list(json.loads(skill["scripts_json"]).keys()) if skill.get("scripts_json") else [],
        "references": list(json.loads(skill["references_json"]).keys()) if skill.get("references_json") else [],
    }


class SkillCreate(BaseModel):
    name: str
    description: str
    prompt: str


@router.post("/api/skills")
async def create_skill(request: Request, body: SkillCreate, user: dict = Depends(require_auth)):
    """Create a custom skill."""
    db = get_db(request)
    user_id = get_user_id(request)

    # Build a valid SKILL.md from the user input
    skill_md = f"""---
name: {body.name}
description: {body.description}
---

{body.prompt}
"""
    # Validate it parses correctly
    try:
        parse_skill_md(skill_md)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    skill_id = f"user-{uuid.uuid4().hex[:12]}"
    await db.create_skill(
        skill_id=skill_id,
        user_id=user_id,
        name=body.name,
        description=body.description,
        source="custom",
        skill_md=skill_md,
    )
    invalidate_skill_cache(skill_id)
    invalidate_graph_cache()
    return {"id": skill_id, "name": body.name, "description": body.description, "source": "custom"}


class SkillInstall(BaseModel):
    repo: str  # "owner/repo" or "owner/repo/path"
    path: str | None = None  # Optional subdirectory path


@router.post("/api/skills/install")
async def install_skill(request: Request, body: SkillInstall, user: dict = Depends(require_auth)):
    """Install a skill from a GitHub repository."""
    db = get_db(request)
    user_id = get_user_id(request)

    # Parse repo and path
    parts = body.repo.split("/", 2)
    if len(parts) < 2:
        raise HTTPException(status_code=400, detail="repo must be 'owner/repo' or 'owner/repo/path'")
    owner, repo = parts[0], parts[1]
    path = body.path or (parts[2] if len(parts) > 2 else "")

    # Build the raw URL for SKILL.md
    skill_md_path = f"{path}/SKILL.md" if path else "SKILL.md"
    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/{skill_md_path}"

    async with httpx.AsyncClient(timeout=15) as client:
        # Fetch SKILL.md
        resp = await client.get(raw_url)
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail=f"SKILL.md not found at {owner}/{repo}/{skill_md_path}")
        if resp.status_code == 403:
            raise HTTPException(status_code=429, detail="GitHub rate limit exceeded, try again later")
        resp.raise_for_status()
        skill_md = resp.text

    # Validate
    try:
        parsed = parse_skill_md(skill_md)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid SKILL.md: {e}") from e

    # Fetch companion files via GitHub tree API
    scripts_json = None
    references_json = None
    assets_json = None

    async with httpx.AsyncClient(timeout=15) as client:
        base_api = f"https://api.github.com/repos/{owner}/{repo}/contents"
        base_path = path or ""

        for dirname, target in [("scripts", "scripts_json"), ("references", "references_json")]:
            dir_path = f"{base_path}/{dirname}" if base_path else dirname
            dir_resp = await client.get(f"{base_api}/{dir_path}")
            if dir_resp.status_code != 200:
                continue
            files = dir_resp.json()
            if not isinstance(files, list):
                continue
            contents = {}
            for f in files:
                if f.get("type") != "file":
                    continue
                file_resp = await client.get(f["download_url"])
                if file_resp.status_code == 200:
                    contents[f["name"]] = file_resp.text
            if contents:
                if target == "scripts_json":
                    scripts_json = json.dumps(contents)
                elif target == "references_json":
                    references_json = json.dumps(contents)

    source_ref = f"{owner}/{repo}/{path}" if path else f"{owner}/{repo}"
    skill_id = f"gh-{uuid.uuid4().hex[:12]}"
    await db.create_skill(
        skill_id=skill_id,
        user_id=user_id,
        name=parsed.name,
        description=parsed.description,
        source="github",
        source_ref=source_ref,
        skill_md=skill_md,
        scripts_json=scripts_json,
        references_json=references_json,
        assets_json=assets_json,
    )
    invalidate_skill_cache(skill_id)
    invalidate_graph_cache()
    return {
        "id": skill_id,
        "name": parsed.name,
        "description": parsed.description,
        "source": "github",
        "source_ref": source_ref,
    }


class SkillUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    prompt: str | None = None
    enabled: bool | None = None


@router.put("/api/skills/{skill_id}")
async def update_skill(request: Request, skill_id: str, body: SkillUpdate, user: dict = Depends(require_auth)):
    """Update a user skill."""
    db = get_db(request)
    skill = await db.get_skill(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    if skill.get("user_id") is None:
        raise HTTPException(status_code=403, detail="Cannot modify system skills")
    if skill.get("user_id") != get_user_id(request):
        raise HTTPException(status_code=403, detail="Not your skill")

    fields: dict = {}
    if body.enabled is not None:
        fields["enabled"] = body.enabled
    if body.name is not None:
        fields["name"] = body.name
    if body.description is not None:
        fields["description"] = body.description

    # If prompt changed, rebuild the SKILL.md
    if body.prompt is not None:
        name = body.name or skill["name"]
        description = body.description or skill["description"]
        skill_md = f"""---
name: {name}
description: {description}
---

{body.prompt}
"""
        try:
            parse_skill_md(skill_md)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        fields["skill_md"] = skill_md

    await db.update_skill(skill_id, **fields)
    invalidate_skill_cache(skill_id)
    invalidate_graph_cache()
    return {"ok": True}


@router.delete("/api/skills/{skill_id}")
async def delete_skill(request: Request, skill_id: str, user: dict = Depends(require_auth)):
    """Delete a user skill."""
    db = get_db(request)
    skill = await db.get_skill(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    if skill.get("user_id") is None:
        raise HTTPException(status_code=403, detail="Cannot delete system skills")
    if skill.get("user_id") != get_user_id(request):
        raise HTTPException(status_code=403, detail="Not your skill")

    await db.delete_skill(skill_id, get_user_id(request))
    invalidate_skill_cache(skill_id)
    invalidate_graph_cache()
    return {"ok": True}
