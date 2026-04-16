"""Tests for the missing-credentials computation in the skills list route.

Covers ``_required_env_for`` and the ``_skill_summary`` projection that
list_skills uses to surface which declared credentials a user hasn't set
yet. The route handler itself isn't exercised here (it requires DB +
auth wiring); the helpers are pure and that's what could break.
"""

from rhiza_agents.routes.skills import _required_env_for, _skill_summary

_SKILL_WITH_REQS = """---
name: needs-creds
description: A skill declaring its credential requirements.
metadata:
  openclaw:
    requires:
      env:
        - MATON_API_KEY
        - TAHMO_USERNAME
---
Body.
"""

_SKILL_NO_REQS = """---
name: plain-skill
description: No openclaw block.
---
Body.
"""

_MALFORMED = "this is not a SKILL.md"


def test_required_env_for_returns_declared_names():
    skill = {"skill_md": _SKILL_WITH_REQS}
    assert _required_env_for(skill) == ["MATON_API_KEY", "TAHMO_USERNAME"]


def test_required_env_for_returns_empty_when_no_block():
    skill = {"skill_md": _SKILL_NO_REQS}
    assert _required_env_for(skill) == []


def test_required_env_for_swallows_parse_errors():
    # Malformed SKILL.md must not crash list_skills — return [] so the row
    # just doesn't get a missing-credentials chip.
    skill = {"skill_md": _MALFORMED}
    assert _required_env_for(skill) == []


def test_required_env_for_handles_missing_skill_md_field():
    assert _required_env_for({}) == []


def test_skill_summary_omits_missing_credentials_when_not_passed():
    """install/refresh responses don't compute missing — the field stays absent."""
    skill = {
        "id": "sk-1",
        "name": "x",
        "description": "x",
        "source": "github",
        "skill_md": _SKILL_WITH_REQS,
    }
    out = _skill_summary(skill)
    assert "missing_credentials" not in out


def test_skill_summary_includes_missing_credentials_when_passed():
    skill = {
        "id": "sk-1",
        "name": "x",
        "description": "x",
        "source": "github",
        "skill_md": _SKILL_WITH_REQS,
    }
    out = _skill_summary(skill, missing_credentials=["MATON_API_KEY"])
    assert out["missing_credentials"] == ["MATON_API_KEY"]


def test_skill_summary_passes_through_empty_missing_list():
    """Empty list is meaningful (skill declares creds, all are set) and
    must round-trip rather than being treated as None."""
    skill = {
        "id": "sk-1",
        "name": "x",
        "description": "x",
        "source": "github",
        "skill_md": _SKILL_WITH_REQS,
    }
    out = _skill_summary(skill, missing_credentials=[])
    assert out["missing_credentials"] == []
