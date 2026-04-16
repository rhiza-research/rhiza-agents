"""GitHub interactions for the skills install/discover/refresh routes.

Concentrates every HTTP call to GitHub in one module so the routes stay
focused on persistence/HTTP-glue and the GitHub-shape concerns (refs vs
SHAs, contents API vs raw, default-branch resolution, optional auth via
``GITHUB_TOKEN``) live in one place.

Anonymous GitHub API has a 60/hr per-IP rate limit, which bulk install +
refresh + multi-skill discover blow through fast. Set ``GITHUB_TOKEN`` in
the environment to raise the quota to 5000/hr.

The functions here are async coroutines that take an injected ``httpx.AsyncClient``
so the routes can share a connection pool and tests can patch the client
with a mock.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import httpx

_GITHUB_API = "https://api.github.com"
_RAW = "https://raw.githubusercontent.com"


def _headers() -> dict[str, str]:
    """Return default headers, including the GITHUB_TOKEN if set.

    Anonymous GitHub API is rate-limited at 60/hr per IP. With a token it's
    5000/hr — bulk install + refresh easily blow past the anonymous quota,
    so we honor a ``GITHUB_TOKEN`` env var when present.
    """
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


@dataclass
class SkillManifest:
    """A discovered skill before any install side-effects."""

    subpath: str  # path inside the repo, e.g. "skills/ecmwf-fetch"
    name: str
    description: str
    license: str | None
    compatibility: str | None
    has_scripts: bool
    # Credential names declared via ``metadata.openclaw.requires.env`` in
    # SKILL.md. Empty when the skill omits the block. Surfaced through
    # ``/api/skills/discover`` so the install UI can warn when a skill
    # needs credentials the user has not yet set.
    required_env: list[str] = field(default_factory=list)


@dataclass
class SkillContents:
    """A fully-fetched skill ready to persist."""

    subpath: str
    name: str
    description: str
    skill_md: str
    scripts_json: str | None
    references_json: str | None
    assets_json: str | None


class GitHubError(Exception):
    """Raised for GitHub-side problems (404, rate limit, etc.) with a user-readable message."""


async def resolve_default_branch(client: httpx.AsyncClient, owner: str, repo: str) -> str:
    """Return the repo's default branch name (e.g. 'main' or 'master')."""
    resp = await client.get(f"{_GITHUB_API}/repos/{owner}/{repo}", headers=_headers())
    if resp.status_code == 404:
        raise GitHubError(f"repo {owner}/{repo} not found")
    if resp.status_code == 403:
        raise GitHubError("GitHub rate limit exceeded — set GITHUB_TOKEN to raise the quota")
    resp.raise_for_status()
    return resp.json().get("default_branch") or "main"


async def resolve_sha(client: httpx.AsyncClient, owner: str, repo: str, ref: str) -> str:
    """Resolve a branch / tag / partial-sha into a full commit SHA."""
    resp = await client.get(f"{_GITHUB_API}/repos/{owner}/{repo}/commits/{ref}", headers=_headers())
    if resp.status_code == 404:
        raise GitHubError(f"ref {ref!r} not found in {owner}/{repo}")
    if resp.status_code == 403:
        raise GitHubError("GitHub rate limit exceeded — set GITHUB_TOKEN to raise the quota")
    resp.raise_for_status()
    sha = resp.json().get("sha")
    if not sha:
        raise GitHubError(f"GitHub did not return a SHA for {ref!r}")
    return sha


async def fetch_skill_md(client: httpx.AsyncClient, owner: str, repo: str, sha: str, path: str) -> str | None:
    """Fetch ``<path>/SKILL.md`` at the given SHA. Returns None if absent."""
    skill_md_path = f"{path}/SKILL.md".lstrip("/") if path else "SKILL.md"
    url = f"{_RAW}/{owner}/{repo}/{sha}/{skill_md_path}"
    resp = await client.get(url, headers=_headers())
    if resp.status_code == 404:
        return None
    if resp.status_code == 403:
        raise GitHubError("GitHub rate limit exceeded — set GITHUB_TOKEN to raise the quota")
    resp.raise_for_status()
    return resp.text


async def fetch_companion_dir(
    client: httpx.AsyncClient, owner: str, repo: str, sha: str, dir_path: str
) -> dict[str, str] | None:
    """Fetch every file under ``<dir_path>`` at the given SHA, as ``{name: content}``.

    Returns None if the directory doesn't exist. Used for ``scripts/`` and
    ``references/`` companion dirs.
    """
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/contents/{dir_path}?ref={sha}"
    resp = await client.get(url, headers=_headers())
    if resp.status_code == 404:
        return None
    if resp.status_code == 403:
        raise GitHubError("GitHub rate limit exceeded — set GITHUB_TOKEN to raise the quota")
    resp.raise_for_status()
    files = resp.json()
    if not isinstance(files, list):
        return None
    contents: dict[str, str] = {}
    for f in files:
        if f.get("type") != "file":
            continue
        download_url = f.get("download_url")
        if not download_url:
            continue
        file_resp = await client.get(download_url, headers=_headers())
        if file_resp.status_code == 200:
            contents[f["name"]] = file_resp.text
    return contents or None


async def list_subdir_skills(
    client: httpx.AsyncClient, owner: str, repo: str, sha: str, path: str
) -> tuple[list[str], list[tuple[str, str]]]:
    """List immediate subdirectories of ``<path>`` that contain a SKILL.md.

    Returns ``(skill_subpaths, skipped)`` where ``skill_subpaths`` is a list
    of full repo paths (e.g. ``"skills/ecmwf-fetch"``) and ``skipped`` is a
    list of ``(subpath, reason)`` tuples for entries that were not skills.

    Does not recurse — only direct children of ``path``.
    """
    list_path = path.lstrip("/") if path else ""
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/contents/{list_path}?ref={sha}"
    resp = await client.get(url, headers=_headers())
    if resp.status_code == 404:
        raise GitHubError(f"path {path!r} not found in {owner}/{repo}@{sha[:7]}")
    if resp.status_code == 403:
        raise GitHubError("GitHub rate limit exceeded — set GITHUB_TOKEN to raise the quota")
    resp.raise_for_status()
    entries = resp.json()
    if not isinstance(entries, list):
        raise GitHubError(f"path {path!r} is a file, not a directory")

    skill_subpaths: list[str] = []
    skipped: list[tuple[str, str]] = []
    for entry in entries:
        if entry.get("type") != "dir":
            continue
        subpath = entry["path"]  # full repo path, e.g. "skills/ecmwf-fetch"
        skill_md = await fetch_skill_md(client, owner, repo, sha, subpath)
        if skill_md is None:
            skipped.append((subpath, "no SKILL.md"))
            continue
        skill_subpaths.append(subpath)
    return skill_subpaths, skipped


async def fetch_skill_contents(
    client: httpx.AsyncClient, owner: str, repo: str, sha: str, subpath: str
) -> SkillContents:
    """Fetch a complete skill (SKILL.md + scripts/ + references/) at a SHA.

    Raises ``GitHubError`` if the SKILL.md is missing.
    """
    from .agents.tools.skills import parse_skill_md

    skill_md = await fetch_skill_md(client, owner, repo, sha, subpath)
    if skill_md is None:
        raise GitHubError(f"no SKILL.md at {subpath!r}")
    parsed = parse_skill_md(skill_md)

    scripts = await fetch_companion_dir(client, owner, repo, sha, f"{subpath}/scripts")
    refs = await fetch_companion_dir(client, owner, repo, sha, f"{subpath}/references")

    return SkillContents(
        subpath=subpath,
        name=parsed.name,
        description=parsed.description,
        skill_md=skill_md,
        scripts_json=json.dumps(scripts) if scripts else None,
        references_json=json.dumps(refs) if refs else None,
        assets_json=None,
    )


async def discover_skills(
    client: httpx.AsyncClient, owner: str, repo: str, sha: str, path: str
) -> tuple[list[SkillManifest], list[tuple[str, str]]]:
    """Discover skills under ``<path>`` at the given SHA. Returns ``(manifests, skipped)``.

    The discover step is intentionally lightweight: it only fetches each
    candidate's SKILL.md (one HTTP per candidate), not its scripts or
    references. That keeps preview cheap when the user is just browsing.
    """
    from .agents.tools.skills import parse_skill_md

    # Try the direct path first — single-skill mode.
    direct_md = await fetch_skill_md(client, owner, repo, sha, path)
    if direct_md is not None:
        parsed = parse_skill_md(direct_md)
        scripts = await fetch_companion_dir(
            client, owner, repo, sha, f"{path.rstrip('/')}/scripts" if path else "scripts"
        )
        return (
            [
                SkillManifest(
                    subpath=path or "",
                    name=parsed.name,
                    description=parsed.description,
                    license=parsed.license,
                    compatibility=parsed.compatibility,
                    has_scripts=bool(scripts),
                    required_env=list(parsed.required_env),
                )
            ],
            [],
        )

    # Otherwise treat ``path`` as a container of skills.
    subpaths, skipped = await list_subdir_skills(client, owner, repo, sha, path)
    manifests: list[SkillManifest] = []
    for sp in subpaths:
        skill_md = await fetch_skill_md(client, owner, repo, sha, sp)
        if skill_md is None:
            skipped.append((sp, "no SKILL.md"))
            continue
        try:
            parsed = parse_skill_md(skill_md)
        except ValueError as exc:
            skipped.append((sp, f"invalid SKILL.md: {exc}"))
            continue
        scripts = await fetch_companion_dir(client, owner, repo, sha, f"{sp}/scripts")
        manifests.append(
            SkillManifest(
                subpath=sp,
                name=parsed.name,
                description=parsed.description,
                license=parsed.license,
                compatibility=parsed.compatibility,
                has_scripts=bool(scripts),
                required_env=list(parsed.required_env),
            )
        )
    return manifests, skipped
