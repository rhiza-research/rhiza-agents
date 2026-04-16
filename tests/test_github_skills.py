"""Tests for github_skills helpers — pure logic with a mocked HTTP client.

Daytona/GitHub aren't touched. Each test stubs ``httpx.AsyncClient`` with
canned responses keyed off the URL path, then asserts the helper picks the
right URLs and reads the right fields from the response body.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from rhiza_agents.github_skills import (
    GitHubError,
    _headers,
    discover_skills,
    fetch_companion_dir,
    fetch_skill_contents,
    fetch_skill_md,
    list_subdir_skills,
    resolve_default_branch,
    resolve_sha,
)


@dataclass
class _Resp:
    status_code: int
    _json: Any = None
    text: str = ""

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=None,  # type: ignore[arg-type]
                response=None,  # type: ignore[arg-type]
            )


class _StubClient:
    """Stand-in for ``httpx.AsyncClient`` that resolves URLs via a routing dict."""

    def __init__(self, routes: dict[str, _Resp]):
        self.routes = routes
        self.calls: list[str] = []

    async def get(self, url: str, headers: dict | None = None) -> _Resp:  # noqa: ARG002
        self.calls.append(url)
        if url in self.routes:
            return self.routes[url]
        return _Resp(status_code=404)


def _run(coro):
    return asyncio.run(coro)


# --- _headers (GITHUB_TOKEN handling) ---------------------------------


def test_headers_anonymous_when_token_unset(monkeypatch):
    """No Authorization header when GITHUB_TOKEN is missing — token is OPTIONAL."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    h = _headers()
    assert "Authorization" not in h
    assert h["Accept"] == "application/vnd.github+json"


def test_headers_anonymous_when_token_empty(monkeypatch):
    """Empty / whitespace-only token is treated the same as unset."""
    monkeypatch.setenv("GITHUB_TOKEN", "   ")
    h = _headers()
    assert "Authorization" not in h


def test_headers_includes_bearer_when_token_set(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_example")
    h = _headers()
    assert h["Authorization"] == "Bearer ghp_example"


# --- resolve_default_branch / resolve_sha ------------------------------


def test_resolve_default_branch_returns_repo_default():
    client = _StubClient({"https://api.github.com/repos/o/r": _Resp(200, _json={"default_branch": "develop"})})
    assert _run(resolve_default_branch(client, "o", "r")) == "develop"


def test_resolve_default_branch_falls_back_to_main_when_field_missing():
    client = _StubClient({"https://api.github.com/repos/o/r": _Resp(200, _json={})})
    assert _run(resolve_default_branch(client, "o", "r")) == "main"


def test_resolve_default_branch_raises_for_404():
    client = _StubClient({})
    with pytest.raises(GitHubError, match="not found"):
        _run(resolve_default_branch(client, "o", "missing"))


def test_resolve_default_branch_raises_for_rate_limit():
    client = _StubClient({"https://api.github.com/repos/o/r": _Resp(403, _json={})})
    with pytest.raises(GitHubError, match="rate limit"):
        _run(resolve_default_branch(client, "o", "r"))


def test_resolve_sha_returns_full_sha():
    client = _StubClient(
        {"https://api.github.com/repos/o/r/commits/main": _Resp(200, _json={"sha": "deadbeefdeadbeefdeadbeef"})}
    )
    assert _run(resolve_sha(client, "o", "r", "main")) == "deadbeefdeadbeefdeadbeef"


def test_resolve_sha_404():
    client = _StubClient({})
    with pytest.raises(GitHubError, match="ref"):
        _run(resolve_sha(client, "o", "r", "no-such-branch"))


# --- fetch_skill_md ---------------------------------------------------


def test_fetch_skill_md_at_path_returns_text():
    sha = "abc123"
    client = _StubClient(
        {
            f"https://raw.githubusercontent.com/o/r/{sha}/skills/x/SKILL.md": _Resp(
                200, text="---\nname: x\ndescription: t\n---\nbody"
            )
        }
    )
    out = _run(fetch_skill_md(client, "o", "r", sha, "skills/x"))
    assert out is not None and "name: x" in out


def test_fetch_skill_md_at_root():
    sha = "abc"
    client = _StubClient({f"https://raw.githubusercontent.com/o/r/{sha}/SKILL.md": _Resp(200, text="ok")})
    assert _run(fetch_skill_md(client, "o", "r", sha, "")) == "ok"


def test_fetch_skill_md_returns_none_on_404():
    client = _StubClient({})
    assert _run(fetch_skill_md(client, "o", "r", "abc", "missing")) is None


# --- fetch_companion_dir ----------------------------------------------


def test_fetch_companion_dir_collects_files():
    sha = "abc"
    base = f"https://api.github.com/repos/o/r/contents/skills/x/scripts?ref={sha}"
    client = _StubClient(
        {
            base: _Resp(
                200,
                _json=[
                    {"type": "file", "name": "fetch.py", "download_url": "raw://fetch"},
                    {"type": "file", "name": "plot.py", "download_url": "raw://plot"},
                    {"type": "dir", "name": "subdir"},  # skipped
                ],
            ),
            "raw://fetch": _Resp(200, text="print('fetch')"),
            "raw://plot": _Resp(200, text="print('plot')"),
        }
    )
    out = _run(fetch_companion_dir(client, "o", "r", sha, "skills/x/scripts"))
    assert out == {"fetch.py": "print('fetch')", "plot.py": "print('plot')"}


def test_fetch_companion_dir_returns_none_when_missing():
    client = _StubClient({})
    assert _run(fetch_companion_dir(client, "o", "r", "abc", "skills/x/scripts")) is None


# --- list_subdir_skills -----------------------------------------------


def test_list_subdir_skills_includes_only_dirs_with_skill_md():
    sha = "abc"
    contents_url = f"https://api.github.com/repos/o/r/contents/skills?ref={sha}"
    client = _StubClient(
        {
            contents_url: _Resp(
                200,
                _json=[
                    {"type": "dir", "name": "ecmwf-fetch", "path": "skills/ecmwf-fetch"},
                    {"type": "dir", "name": "shared", "path": "skills/shared"},
                    {"type": "file", "name": "README.md", "path": "skills/README.md"},
                ],
            ),
            f"https://raw.githubusercontent.com/o/r/{sha}/skills/ecmwf-fetch/SKILL.md": _Resp(200, text="ok"),
            # 'shared' has no SKILL.md (default 404) — should be skipped.
        }
    )
    skill_subpaths, skipped = _run(list_subdir_skills(client, "o", "r", sha, "skills"))
    assert skill_subpaths == ["skills/ecmwf-fetch"]
    assert skipped == [("skills/shared", "no SKILL.md")]


def test_list_subdir_skills_does_not_recurse():
    """We only check direct children, not nested subdirs."""
    sha = "abc"
    contents_url = f"https://api.github.com/repos/o/r/contents/skills?ref={sha}"
    client = _StubClient(
        {
            contents_url: _Resp(
                200,
                _json=[{"type": "dir", "name": "fetchers", "path": "skills/fetchers"}],
            ),
        }
    )
    skill_subpaths, skipped = _run(list_subdir_skills(client, "o", "r", sha, "skills"))
    assert skill_subpaths == []
    assert skipped == [("skills/fetchers", "no SKILL.md")]
    # We probe each direct child's SKILL.md (allowed) but never descend deeper:
    # any URL that contains '/skills/fetchers/' but isn't the SKILL.md probe
    # itself would be a nested fetch.
    deep_paths = [
        url for url in client.calls if "/skills/fetchers/" in url and not url.endswith("/skills/fetchers/SKILL.md")
    ]
    assert deep_paths == []


def test_list_subdir_skills_404_path_raises():
    client = _StubClient({})
    with pytest.raises(GitHubError, match="not found"):
        _run(list_subdir_skills(client, "o", "r", "abc", "skills"))


# --- discover_skills (single + directory paths) ------------------------


def test_discover_single_skill_path():
    sha = "abc"
    client = _StubClient(
        {
            f"https://raw.githubusercontent.com/o/r/{sha}/skills/x/SKILL.md": _Resp(
                200,
                text="---\nname: x\ndescription: A demo skill.\nlicense: MIT\n---\nbody",
            ),
        }
    )
    manifests, skipped = _run(discover_skills(client, "o", "r", sha, "skills/x"))
    assert len(manifests) == 1
    assert manifests[0].name == "x"
    assert manifests[0].license == "MIT"
    assert manifests[0].has_scripts is False
    assert skipped == []


def test_discover_directory_of_skills():
    sha = "abc"
    contents_url = f"https://api.github.com/repos/o/r/contents/skills?ref={sha}"
    client = _StubClient(
        {
            contents_url: _Resp(
                200,
                _json=[
                    {"type": "dir", "name": "a", "path": "skills/a"},
                    {"type": "dir", "name": "b", "path": "skills/b"},
                    {"type": "dir", "name": "noskill", "path": "skills/noskill"},
                ],
            ),
            f"https://raw.githubusercontent.com/o/r/{sha}/skills/a/SKILL.md": _Resp(
                200, text="---\nname: a\ndescription: A.\n---\n"
            ),
            f"https://raw.githubusercontent.com/o/r/{sha}/skills/b/SKILL.md": _Resp(
                200, text="---\nname: b\ndescription: B.\n---\n"
            ),
            # 'a' ships scripts; 'b' doesn't.
            f"https://api.github.com/repos/o/r/contents/skills/a/scripts?ref={sha}": _Resp(
                200,
                _json=[{"type": "file", "name": "f.py", "download_url": "raw://f"}],
            ),
            "raw://f": _Resp(200, text="x = 1"),
        }
    )
    manifests, skipped = _run(discover_skills(client, "o", "r", sha, "skills"))
    names = sorted(m.name for m in manifests)
    assert names == ["a", "b"]
    a = next(m for m in manifests if m.name == "a")
    b = next(m for m in manifests if m.name == "b")
    assert a.has_scripts is True
    assert b.has_scripts is False
    assert ("skills/noskill", "no SKILL.md") in skipped


def test_discover_surfaces_required_env_from_openclaw_block():
    """``metadata.openclaw.requires.env`` in a discovered SKILL.md flows
    through the SkillManifest so the install preview UI can warn when the
    user is missing credentials."""
    sha = "abc"
    md = (
        "---\n"
        "name: x\n"
        "description: A demo skill that needs creds.\n"
        "metadata:\n"
        "  openclaw:\n"
        "    requires:\n"
        "      env:\n"
        "        - MATON_API_KEY\n"
        "        - TAHMO_USERNAME\n"
        "    primaryEnv: MATON_API_KEY\n"
        "---\nbody"
    )
    client = _StubClient(
        {
            f"https://raw.githubusercontent.com/o/r/{sha}/skills/x/SKILL.md": _Resp(200, text=md),
        }
    )
    manifests, _ = _run(discover_skills(client, "o", "r", sha, "skills/x"))
    assert len(manifests) == 1
    assert manifests[0].required_env == ["MATON_API_KEY", "TAHMO_USERNAME"]


def test_discover_required_env_empty_when_block_absent():
    """Skills without the block get an empty list, not None — keeps the
    JSON shape predictable for the frontend."""
    sha = "abc"
    client = _StubClient(
        {
            f"https://raw.githubusercontent.com/o/r/{sha}/skills/x/SKILL.md": _Resp(
                200, text="---\nname: x\ndescription: No creds.\n---\nbody"
            ),
        }
    )
    manifests, _ = _run(discover_skills(client, "o", "r", sha, "skills/x"))
    assert manifests[0].required_env == []


def test_discover_skips_invalid_skill_md():
    sha = "abc"
    contents_url = f"https://api.github.com/repos/o/r/contents/skills?ref={sha}"
    client = _StubClient(
        {
            contents_url: _Resp(
                200,
                _json=[{"type": "dir", "name": "bad", "path": "skills/bad"}],
            ),
            f"https://raw.githubusercontent.com/o/r/{sha}/skills/bad/SKILL.md": _Resp(
                200, text="not actually a SKILL.md (no frontmatter)"
            ),
        }
    )
    manifests, skipped = _run(discover_skills(client, "o", "r", sha, "skills"))
    assert manifests == []
    assert len(skipped) == 1
    assert skipped[0][0] == "skills/bad"
    assert "invalid SKILL.md" in skipped[0][1]


# --- fetch_skill_contents (used by install + refresh) -----------------


def test_fetch_skill_contents_bundles_skill_md_scripts_refs():
    sha = "abc"
    client = _StubClient(
        {
            f"https://raw.githubusercontent.com/o/r/{sha}/skills/x/SKILL.md": _Resp(
                200, text="---\nname: x\ndescription: T.\n---\nbody"
            ),
            f"https://api.github.com/repos/o/r/contents/skills/x/scripts?ref={sha}": _Resp(
                200,
                _json=[{"type": "file", "name": "f.py", "download_url": "raw://f"}],
            ),
            "raw://f": _Resp(200, text="x = 1"),
            # No references/ — should land as None.
        }
    )
    contents = _run(fetch_skill_contents(client, "o", "r", sha, "skills/x"))
    assert contents.name == "x"
    assert contents.description == "T."
    assert contents.scripts_json is not None and "f.py" in contents.scripts_json
    assert contents.references_json is None


def test_fetch_skill_contents_raises_when_skill_md_missing():
    client = _StubClient({})
    with pytest.raises(GitHubError, match="no SKILL.md"):
        _run(fetch_skill_contents(client, "o", "r", "abc", "skills/missing"))
