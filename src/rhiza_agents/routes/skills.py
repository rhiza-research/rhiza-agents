"""Skills CRUD and GitHub install/discover/refresh API routes."""

import json
import logging
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..agents.graph import invalidate_graph_cache
from ..agents.tools.skills import parse_skill_md, requires_sandbox
from ..deps import get_db, get_user_id, invalidate_skill_cache, require_auth
from ..github_skills import (
    GitHubError,
    SkillContents,
    discover_skills,
    fetch_skill_contents,
    resolve_default_branch,
    resolve_sha,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["skills"])


def _skill_summary(skill: dict) -> dict:
    """Shared shape for list/get/install/refresh responses."""
    return {
        "id": skill["id"],
        "name": skill["name"],
        "description": skill["description"],
        "source": skill["source"],
        "source_ref": skill.get("source_ref"),
        "source_sha": skill.get("source_sha"),
        "source_branch": skill.get("source_branch"),
        "enabled": bool(skill.get("enabled", True)),
        "system": skill.get("user_id") is None,
        "requires_sandbox": requires_sandbox(skill),
    }


@router.get("/api/skills")
async def list_skills(request: Request, user: dict = Depends(require_auth)):
    """List all skills visible to the user (system + user-owned)."""
    db = get_db(request)
    user_id = get_user_id(request)
    skills = await db.list_skills(user_id)
    return [_skill_summary(s) for s in skills]


@router.get("/api/skills/{skill_id}")
async def get_skill(request: Request, skill_id: str, user: dict = Depends(require_auth)):
    """Get full skill details."""
    db = get_db(request)
    skill = await db.get_skill(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    out = _skill_summary(skill)
    out["skill_md"] = skill["skill_md"]
    out["scripts"] = list(json.loads(skill["scripts_json"]).keys()) if skill.get("scripts_json") else []
    out["references"] = list(json.loads(skill["references_json"]).keys()) if skill.get("references_json") else []
    return out


class SkillCreate(BaseModel):
    name: str
    description: str
    prompt: str


@router.post("/api/skills")
async def create_skill(request: Request, body: SkillCreate, user: dict = Depends(require_auth)):
    """Create a custom skill."""
    db = get_db(request)
    user_id = get_user_id(request)

    skill_md = f"""---
name: {body.name}
description: {body.description}
---

{body.prompt}
"""
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


# --- Discover / install / refresh from GitHub --------------------------


def _parse_repo_arg(repo: str) -> tuple[str, str, str]:
    """Split ``owner/repo`` (optionally ``owner/repo/path``) into its parts.

    The optional inline path lets old single-skill callers continue to work.
    Newer callers should pass ``path`` explicitly.
    """
    parts = repo.split("/", 2)
    if len(parts) < 2:
        raise HTTPException(status_code=400, detail="repo must be 'owner/repo' or 'owner/repo/path'")
    inline_path = parts[2] if len(parts) > 2 else ""
    return parts[0], parts[1], inline_path


async def _resolve_ref_to_sha(client: httpx.AsyncClient, owner: str, repo: str, ref: str | None) -> tuple[str, str]:
    """Resolve a user-supplied ref (or None for default branch) to ``(branch, sha)``.

    The branch is recorded so refresh re-resolves the same branch HEAD,
    even after the user has installed and pushed mid-flight.
    """
    branch = ref or await resolve_default_branch(client, owner, repo)
    sha = await resolve_sha(client, owner, repo, branch)
    return branch, sha


class SkillDiscoverBody(BaseModel):
    repo: str
    path: str | None = None
    ref: str | None = None  # branch / tag / sha; default = repo default branch


@router.post("/api/skills/discover")
async def discover_skills_route(request: Request, body: SkillDiscoverBody, user: dict = Depends(require_auth)):
    """List skills available under a repo path without installing them.

    Returns:
        {
          source_sha, source_branch, source_ref,
          available: [{subpath, name, description, license, compatibility,
                       has_scripts, already_installed: {id, sha} | None}],
          skipped: [{subpath, reason}],
          errors: [...]
        }
    """
    db = get_db(request)
    user_id = get_user_id(request)
    owner, repo, inline_path = _parse_repo_arg(body.repo)
    path = body.path if body.path is not None else inline_path

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            branch, sha = await _resolve_ref_to_sha(client, owner, repo, body.ref)
            manifests, skipped = await discover_skills(client, owner, repo, sha, path)
        except GitHubError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    available = []
    for m in manifests:
        # Source ref persisted on installed skills uses the same shape as
        # below — match against it to mark already-installed entries.
        candidate_ref = f"{owner}/{repo}/{m.subpath}" if m.subpath else f"{owner}/{repo}"
        existing = await db.find_user_skill_by_source_ref(user_id, candidate_ref)
        already = None
        if existing:
            already = {
                "id": existing["id"],
                "sha": existing.get("source_sha"),
            }
        available.append(
            {
                "subpath": m.subpath,
                "name": m.name,
                "description": m.description,
                "license": m.license,
                "compatibility": m.compatibility,
                "has_scripts": m.has_scripts,
                "already_installed": already,
            }
        )

    return {
        "source_sha": sha,
        "source_branch": branch,
        "source_ref": f"{owner}/{repo}",
        "available": available,
        "skipped": [{"subpath": sp, "reason": r} for sp, r in skipped],
        "errors": [],
    }


class SkillInstallBody(BaseModel):
    repo: str
    # New shape: explicit list of subpaths chosen from a discover response.
    paths: list[str] | None = None
    # Legacy single-skill shape kept for backward compat with non-frontend callers.
    path: str | None = None
    ref: str | None = None
    # When true, re-install a skill the user already has (deletes the old row).
    force: bool = False


async def _persist_install(
    db,
    user_id: str,
    owner: str,
    repo: str,
    branch: str,
    sha: str,
    contents: SkillContents,
    *,
    force: bool,
) -> tuple[dict | None, str | None]:
    """Insert a skill row, returning ``(skill_row, error_message)``."""
    source_ref = f"{owner}/{repo}/{contents.subpath}" if contents.subpath else f"{owner}/{repo}"
    existing = await db.find_user_skill_by_source_ref(user_id, source_ref)
    if existing and not force:
        return None, f"already installed (id={existing['id']})"
    if existing and force:
        await db.delete_skill(existing["id"], user_id)
        invalidate_skill_cache(existing["id"])

    skill_id = f"gh-{uuid.uuid4().hex[:12]}"
    row = await db.create_skill(
        skill_id=skill_id,
        user_id=user_id,
        name=contents.name,
        description=contents.description,
        source="github",
        source_ref=source_ref,
        source_sha=sha,
        source_branch=branch,
        skill_md=contents.skill_md,
        scripts_json=contents.scripts_json,
        references_json=contents.references_json,
        assets_json=contents.assets_json,
    )
    invalidate_skill_cache(skill_id)
    return row, None


@router.post("/api/skills/install")
async def install_skill(request: Request, body: SkillInstallBody, user: dict = Depends(require_auth)):
    """Install one or more skills from a GitHub repository at a pinned SHA.

    Accepts either ``paths: [...]`` (preferred — list of skill subpaths
    relative to the repo) or the legacy ``path: "..."`` (single skill).
    All paths are installed at the same commit SHA so they're consistent.
    """
    db = get_db(request)
    user_id = get_user_id(request)
    owner, repo, inline_path = _parse_repo_arg(body.repo)

    paths: list[str]
    if body.paths:
        paths = list(body.paths)
    elif body.path is not None:
        paths = [body.path]
    elif inline_path:
        paths = [inline_path]
    else:
        # No path given — install the repo root if it has a SKILL.md.
        paths = [""]

    installed: list[dict] = []
    errors: list[dict] = []
    skipped: list[dict] = []

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            branch, sha = await _resolve_ref_to_sha(client, owner, repo, body.ref)
        except GitHubError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        for path in paths:
            try:
                contents = await fetch_skill_contents(client, owner, repo, sha, path)
            except GitHubError as e:
                errors.append({"subpath": path, "error": str(e)})
                continue
            except ValueError as e:
                # Bad SKILL.md
                errors.append({"subpath": path, "error": f"invalid SKILL.md: {e}"})
                continue

            row, err = await _persist_install(db, user_id, owner, repo, branch, sha, contents, force=body.force)
            if err:
                skipped.append({"subpath": path, "reason": err})
                continue
            assert row is not None
            installed.append(_skill_summary(row))

    if installed:
        invalidate_graph_cache()

    return {
        "source_sha": sha,
        "source_branch": branch,
        "installed": installed,
        "skipped": skipped,
        "errors": errors,
    }


# --- Refresh -----------------------------------------------------------


def _parse_source_ref(source_ref: str) -> tuple[str, str, str]:
    """Inverse of the source_ref construction used in _persist_install."""
    parts = source_ref.split("/", 2)
    if len(parts) < 2:
        raise GitHubError(f"malformed source_ref {source_ref!r}")
    owner, repo = parts[0], parts[1]
    subpath = parts[2] if len(parts) > 2 else ""
    return owner, repo, subpath


async def _refresh_one(db, client: httpx.AsyncClient, skill: dict) -> dict:
    """Refresh a single skill from upstream. Returns a result entry.

    Result shape: ``{id, name, status, sha, prev_sha?, error?}`` where
    ``status`` is one of ``"updated"``, ``"unchanged"``, ``"error"``,
    or ``"skipped"`` (e.g. for source=custom).
    """
    if skill.get("source") != "github" or not skill.get("source_ref"):
        return {
            "id": skill["id"],
            "name": skill["name"],
            "status": "skipped",
            "error": "skill is not GitHub-sourced",
        }

    try:
        owner, repo, subpath = _parse_source_ref(skill["source_ref"])
    except GitHubError as e:
        return {"id": skill["id"], "name": skill["name"], "status": "error", "error": str(e)}

    branch = skill.get("source_branch")
    try:
        if not branch:
            branch = await resolve_default_branch(client, owner, repo)
        latest_sha = await resolve_sha(client, owner, repo, branch)
    except GitHubError as e:
        return {"id": skill["id"], "name": skill["name"], "status": "error", "error": str(e)}

    prev_sha = skill.get("source_sha")
    if prev_sha and latest_sha == prev_sha:
        return {
            "id": skill["id"],
            "name": skill["name"],
            "status": "unchanged",
            "sha": latest_sha,
        }

    try:
        contents = await fetch_skill_contents(client, owner, repo, latest_sha, subpath)
    except (GitHubError, ValueError) as e:
        return {"id": skill["id"], "name": skill["name"], "status": "error", "error": str(e)}

    # Compare per-file content. Only update if anything actually changed,
    # so a same-content reinstall (e.g. SHA bumped by an unrelated commit)
    # doesn't create churn.
    same = (
        contents.skill_md == skill.get("skill_md")
        and contents.scripts_json == skill.get("scripts_json")
        and contents.references_json == skill.get("references_json")
        and contents.assets_json == skill.get("assets_json")
    )
    if same and prev_sha:
        # Bump the recorded SHA + branch but report unchanged content.
        await db.update_skill(skill["id"], source_sha=latest_sha, source_branch=branch)
        return {
            "id": skill["id"],
            "name": skill["name"],
            "status": "unchanged",
            "sha": latest_sha,
            "prev_sha": prev_sha,
        }

    await db.update_skill(
        skill["id"],
        name=contents.name,
        description=contents.description,
        skill_md=contents.skill_md,
        scripts_json=contents.scripts_json,
        references_json=contents.references_json,
        assets_json=contents.assets_json,
        source_sha=latest_sha,
        source_branch=branch,
    )
    invalidate_skill_cache(skill["id"])
    return {
        "id": skill["id"],
        "name": contents.name,
        "status": "updated",
        "sha": latest_sha,
        "prev_sha": prev_sha,
    }


@router.post("/api/skills/{skill_id}/refresh")
async def refresh_skill(request: Request, skill_id: str, user: dict = Depends(require_auth)):
    """Re-pull a skill from its source, preserving id and per-skill state."""
    db = get_db(request)
    skill = await db.get_skill(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    if skill.get("user_id") is None:
        raise HTTPException(status_code=403, detail="Cannot refresh system skills")
    if skill.get("user_id") != get_user_id(request):
        raise HTTPException(status_code=403, detail="Not your skill")

    async with httpx.AsyncClient(timeout=30) as client:
        result = await _refresh_one(db, client, skill)

    if result.get("status") == "updated":
        invalidate_graph_cache()
    return result


@router.post("/api/skills/refresh")
async def refresh_all_skills(request: Request, user: dict = Depends(require_auth)):
    """Refresh every GitHub-sourced skill the user owns.

    Returns ``{updated, unchanged, errors, skipped}``. Each entry is a
    per-skill result from ``_refresh_one``.
    """
    db = get_db(request)
    user_id = get_user_id(request)
    skills = await db.list_user_github_skills(user_id)

    updated: list[dict] = []
    unchanged: list[dict] = []
    errors: list[dict] = []
    skipped: list[dict] = []

    async with httpx.AsyncClient(timeout=30) as client:
        for skill in skills:
            result = await _refresh_one(db, client, skill)
            if result["status"] == "updated":
                updated.append(result)
            elif result["status"] == "unchanged":
                unchanged.append(result)
            elif result["status"] == "skipped":
                skipped.append(result)
            else:
                errors.append(result)

    if updated:
        invalidate_graph_cache()
    return {
        "updated": updated,
        "unchanged": unchanged,
        "errors": errors,
        "skipped": skipped,
    }


# --- Update / delete (unchanged) ---------------------------------------


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
