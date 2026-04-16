"""Tests for skill tool activation (Agent Skills support).

Covers the pure parsing helpers and the activation Command built by
``create_skill_tool`` — no sandbox, no LangGraph graph build.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from rhiza_agents.agents.tools.skills import (
    _build_activation_response,
    _parsed_scripts,
    create_skill_tool,
    parse_skill_md,
    requires_sandbox,
)

SKILL_MD_MINIMAL = """---
name: hello-skill
description: A demo skill that prints hello. Use when demoing.
---
# Hello

Body content.
"""

SKILL_MD_WITH_REFS = """---
name: ref-skill
description: A skill with reference docs.
---
# Ref

Body.
"""


def _record(skill_md=SKILL_MD_MINIMAL, scripts=None, refs=None):
    return {
        "id": "sk-test",
        "skill_md": skill_md,
        "scripts_json": scripts,
        "references_json": refs,
        "assets_json": None,
    }


# -- _parsed_scripts -----------------------------------------------------


def test_parsed_scripts_none():
    assert _parsed_scripts(_record(scripts=None)) == {}


def test_parsed_scripts_empty_dict():
    assert _parsed_scripts(_record(scripts={})) == {}


def test_parsed_scripts_dict_input():
    out = _parsed_scripts(_record(scripts={"a.py": "print(1)", "b.py": "print(2)"}))
    assert out == {"a.py": "print(1)", "b.py": "print(2)"}


def test_parsed_scripts_json_string_input():
    raw = json.dumps({"fetch.py": "x = 1"})
    out = _parsed_scripts(_record(scripts=raw))
    assert out == {"fetch.py": "x = 1"}


# -- _build_activation_response ------------------------------------------


def test_activation_response_contains_body():
    resp = _build_activation_response(_record())
    assert "# Hello" in resp
    assert "Body content." in resp


def test_activation_response_inlines_references():
    refs = json.dumps({"ENVELOPE.md": "# Envelope\nSpec body."})
    resp = _build_activation_response(_record(refs=refs))
    assert "## Reference: ENVELOPE.md" in resp
    assert "Spec body." in resp


def test_activation_response_mentions_each_script_path():
    resp = _build_activation_response(_record(scripts={"fetch.py": "print(1)", "plot.py": "print(2)"}))
    # Paths are namespaced by skill name to avoid collisions across skills.
    assert "/skills/hello-skill/scripts/fetch.py" in resp
    assert "/skills/hello-skill/scripts/plot.py" in resp
    assert "run_file" in resp


def test_activation_response_has_no_script_note_when_empty():
    resp = _build_activation_response(_record())
    assert "/scripts/" not in resp
    assert "/skills/" not in resp


# -- create_skill_tool + activation Command ------------------------------


def _activate(tool, state=None):
    runtime = SimpleNamespace(tool_call_id="call-xyz", state=state or {})
    return asyncio.run(tool.coroutine(runtime=runtime))


def test_tool_name_and_description():
    tool = create_skill_tool(_record())
    assert tool.name == "skill_hello_skill"
    assert tool.description == "Skill: A demo skill that prints hello. Use when demoing."


def test_tool_metadata_no_scripts():
    tool = create_skill_tool(_record())
    assert tool.metadata["skill_id"] == "sk-test"
    assert tool.metadata["requires_sandbox"] is False


def test_tool_metadata_with_scripts_requires_sandbox():
    tool = create_skill_tool(_record(scripts={"a.py": "pass"}))
    assert tool.metadata["requires_sandbox"] is True


def test_activation_command_no_scripts_has_only_messages():
    tool = create_skill_tool(_record())
    cmd = _activate(tool)
    assert "files" not in cmd.update
    assert len(cmd.update["messages"]) == 1
    msg = cmd.update["messages"][0]
    assert msg.tool_call_id == "call-xyz"
    assert "# Hello" in msg.content


def test_activation_command_loads_scripts_into_files():
    tool = create_skill_tool(_record(scripts={"fetch.py": "print('hi')\n", "plot.py": "print('bye')"}))
    cmd = _activate(tool)
    files = cmd.update["files"]
    # Paths are namespaced under /skills/<skill-name>/scripts so that
    # multiple skills shipping the same filename (e.g. four fetchers each
    # with a fetch.py) don't collide in the file state.
    assert set(files.keys()) == {
        "/skills/hello-skill/scripts/fetch.py",
        "/skills/hello-skill/scripts/plot.py",
    }
    # Stored as a list of lines, matching write_file's shape.
    assert files["/skills/hello-skill/scripts/fetch.py"]["content"] == ["print('hi')", ""]
    assert files["/skills/hello-skill/scripts/plot.py"]["content"] == ["print('bye')"]
    # Metadata stamped for provenance.
    for entry in files.values():
        assert entry["source"] == "skill"
        assert entry["skill_id"] == "sk-test"
        assert "created_at" in entry and "modified_at" in entry


def test_activation_namespaces_by_skill_name():
    """Two skills with a filename collision must not stomp each other."""
    ecmwf = create_skill_tool(
        {
            "id": "sk-ecmwf",
            "skill_md": "---\nname: ecmwf-fetch\ndescription: ECMWF fetcher.\n---\nbody\n",
            "scripts_json": {"fetch.py": "print('ecmwf')"},
        }
    )
    chirps = create_skill_tool(
        {
            "id": "sk-chirps",
            "skill_md": "---\nname: chirps-fetch\ndescription: CHIRPS fetcher.\n---\nbody\n",
            "scripts_json": {"fetch.py": "print('chirps')"},
        }
    )
    e_files = _activate(ecmwf).update["files"]
    c_files = _activate(chirps).update["files"]
    assert "/skills/ecmwf-fetch/scripts/fetch.py" in e_files
    assert "/skills/chirps-fetch/scripts/fetch.py" in c_files
    # Crucially they land at distinct paths so activating both in the same
    # conversation keeps both scripts available.
    assert set(e_files).isdisjoint(set(c_files))


def test_activation_preserves_script_content_passed_as_list():
    # If the DB serializer ever stores content pre-split, we should still
    # round-trip without double-splitting or mangling.
    tool = create_skill_tool(_record(scripts={"a.py": ["line1", "line2"]}))
    cmd = _activate(tool)
    assert cmd.update["files"]["/skills/hello-skill/scripts/a.py"]["content"] == ["line1", "line2"]


def test_activation_tool_message_names_every_script():
    tool = create_skill_tool(_record(scripts={"a.py": "pass", "b.py": "pass"}))
    cmd = _activate(tool)
    content = cmd.update["messages"][0].content
    assert "/skills/hello-skill/scripts/a.py" in content
    assert "/skills/hello-skill/scripts/b.py" in content


@pytest.mark.parametrize(
    "bad_md",
    [
        "no frontmatter",
        "---\nname: x\n---\nno description",
        "---\ndescription: x\n---\nno name",
        "---\nname: Bad Name\ndescription: x\n---\nbad name case",
    ],
)
def test_create_skill_tool_rejects_invalid_skill_md(bad_md):
    with pytest.raises(ValueError):
        create_skill_tool({"id": "x", "skill_md": bad_md})


# -- metadata.openclaw.requires parsing ----------------------------------


_OPENCLAW_MD = """---
name: needs-creds
description: A skill that declares its credential requirements.
metadata:
  openclaw:
    requires:
      env:
        - MATON_API_KEY
        - TAHMO_USERNAME
      bins:
        - git
    primaryEnv: MATON_API_KEY
---
Body.
"""


def test_parse_skill_md_reads_openclaw_requires():
    parsed = parse_skill_md(_OPENCLAW_MD)
    assert parsed.required_env == ["MATON_API_KEY", "TAHMO_USERNAME"]
    assert parsed.primary_env == "MATON_API_KEY"
    assert parsed.required_bins == ["git"]


@pytest.mark.parametrize("alias", ["openclaw", "clawdbot", "clawdis"])
def test_parse_skill_md_accepts_metadata_aliases(alias):
    md = f"""---
name: aliased
description: Skill using the {alias} alias for its requirements block.
metadata:
  {alias}:
    requires:
      env:
        - ALPHA
        - BETA
---
Body.
"""
    parsed = parse_skill_md(md)
    assert parsed.required_env == ["ALPHA", "BETA"]


def test_parse_skill_md_missing_metadata_block_is_empty():
    parsed = parse_skill_md(SKILL_MD_MINIMAL)
    assert parsed.required_env == []
    assert parsed.primary_env is None
    assert parsed.required_bins == []


@pytest.mark.parametrize(
    "mangled_md",
    [
        # requires is not a dict
        "---\nname: x\ndescription: d\nmetadata:\n  openclaw:\n    requires: not-a-dict\n---\nB",
        # env is a scalar instead of a list
        "---\nname: x\ndescription: d\nmetadata:\n  openclaw:\n    requires:\n      env: FOO\n---\nB",
        # primaryEnv is not a string
        "---\nname: x\ndescription: d\nmetadata:\n  openclaw:\n    primaryEnv: [a, b]\n---\nB",
        # openclaw block is a scalar
        "---\nname: x\ndescription: d\nmetadata:\n  openclaw: garbage\n---\nB",
    ],
)
def test_parse_skill_md_tolerates_mangled_openclaw(mangled_md):
    # Should not raise — malformed extensions are treated as absent so a
    # ClawHub-shaped typo doesn't brick the whole skill install.
    parsed = parse_skill_md(mangled_md)
    assert parsed.required_env == []
    assert parsed.required_bins == []
    assert parsed.primary_env is None


def test_parse_skill_md_openclaw_does_not_corrupt_stringified_metadata():
    """The stringified ``metadata`` dict is unchanged by our new parsing.

    Other callers may rely on ``metadata`` being a flat ``dict[str, str]``
    (e.g. version extraction). We preserve that even when a nested openclaw
    block is present — its stringified form is fine, we don't need its
    contents in ``metadata`` since we've surfaced them as structured fields.
    """
    parsed = parse_skill_md(_OPENCLAW_MD)
    # Every value in metadata remains a string, not a dict/list.
    for v in parsed.metadata.values():
        assert isinstance(v, str)


# -- Activation hint for declared credentials ----------------------------


def test_activation_response_includes_required_env_hint():
    resp = _build_activation_response({"id": "sk", "skill_md": _OPENCLAW_MD})
    assert "This skill requires these credential names" in resp
    assert "MATON_API_KEY" in resp
    assert "TAHMO_USERNAME" in resp
    # The instruction nudges the agent toward the right tool argument shape.
    assert "env_vars" in resp


def test_activation_response_omits_credential_hint_when_empty():
    resp = _build_activation_response(_record())
    assert "This skill requires these credential names" not in resp


# -- requires_sandbox uses parsed.required_bins --------------------------


def test_requires_sandbox_true_for_openclaw_requires_bins():
    md = """---
name: needs-bin
description: A skill that declares a binary dep via the openclaw block.
metadata:
  openclaw:
    requires:
      bins:
        - ffmpeg
---
Body without executable code blocks.
"""
    assert requires_sandbox({"id": "x", "skill_md": md, "scripts_json": None}) is True


def test_requires_sandbox_false_for_inert_skill():
    # No scripts, no execution tools, no binary deps, no exec code blocks.
    assert requires_sandbox({"id": "x", "skill_md": SKILL_MD_MINIMAL, "scripts_json": None}) is False
