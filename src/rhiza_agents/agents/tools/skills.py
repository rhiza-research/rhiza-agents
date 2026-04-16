"""Agent Skills — parse SKILL.md and create LangChain tools.

Follows the Agent Skills standard (https://agentskills.io/specification).
Skills are packaged as SKILL.md files with YAML frontmatter + markdown
instructions, plus optional scripts/, references/, and assets/ directories.

Each skill registers as a LangChain tool using progressive disclosure:
- At graph build time: only name + description are loaded (~100 tokens)
- On activation: the tool returns the full prompt + references + script paths
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import yaml
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

logger = logging.getLogger(__name__)


@dataclass
class ParsedSkill:
    """Parsed SKILL.md content."""

    name: str
    description: str
    prompt: str  # Markdown body after frontmatter
    version: str | None = None
    license: str | None = None
    compatibility: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    # Declared runtime requirements read from ``metadata.openclaw`` (plus the
    # ``clawdbot`` / ``clawdis`` aliases ClawHub publishes under). The block
    # is the spec's ``metadata`` extension point, so skills authored this way
    # remain valid on any Agent Skills runtime that only knows the official
    # fields. See docs/reference on the Agent Skills spec and the ClawHub
    # skill-format.
    required_env: list[str] = field(default_factory=list)
    primary_env: str | None = None
    required_bins: list[str] = field(default_factory=list)


# Keys under ``metadata`` we accept the openclaw block at, in priority order.
# ``openclaw`` is the name we prefer for skills we author ourselves; the
# other two are aliases ClawHub publishes under and we read transparently.
_OPENCLAW_METADATA_KEYS = ("openclaw", "clawdbot", "clawdis")


def _openclaw_block(frontmatter: dict) -> dict:
    """Return the first ``metadata.<alias>`` dict that looks like an openclaw block.

    Returns ``{}`` when the frontmatter has no metadata, no recognized alias,
    or the alias value isn't a dict. Never raises on malformed input — callers
    treat a missing block as "skill has no declared requirements".
    """
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    for key in _OPENCLAW_METADATA_KEYS:
        block = metadata.get(key)
        if isinstance(block, dict):
            return block
    return {}


def _string_list(value) -> list[str]:
    """Coerce a YAML list-of-strings field into a clean ``list[str]``.

    Tolerates: missing field, non-list, mixed types — anything that isn't a
    non-empty string is dropped. This mirrors how the ClawHub schema is used
    in practice: authors sometimes write ``env: FOO`` (scalar) or slip a
    non-string in; we'd rather ignore the garbage than crash the parser.
    """
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v]


def parse_skill_md(content: str) -> ParsedSkill:
    """Parse SKILL.md content (YAML frontmatter + markdown body).

    Raises ValueError if required fields (name, description) are missing.
    """
    # Split frontmatter from body
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL)
    if not match:
        raise ValueError("SKILL.md must have YAML frontmatter delimited by ---")

    frontmatter_str, body = match.groups()
    frontmatter = yaml.safe_load(frontmatter_str) or {}

    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not name:
        raise ValueError("SKILL.md frontmatter must include 'name'")
    if not description:
        raise ValueError("SKILL.md frontmatter must include 'description'")

    # Validate name format per spec: lowercase, hyphens, 1-64 chars
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,63}$", name):
        raise ValueError(f"Skill name must be lowercase alphanumeric with hyphens, 1-64 chars: {name}")

    # Parse allowed-tools if present
    allowed_tools = []
    raw_tools = frontmatter.get("allowed-tools", "")
    if raw_tools:
        allowed_tools = raw_tools.split() if isinstance(raw_tools, str) else list(raw_tools)

    openclaw = _openclaw_block(frontmatter)
    requires = openclaw.get("requires") if isinstance(openclaw.get("requires"), dict) else {}
    primary = openclaw.get("primaryEnv")
    primary_env = primary if isinstance(primary, str) and primary else None

    return ParsedSkill(
        name=name,
        description=description[:1024],
        prompt=body.strip(),
        version=str(frontmatter["metadata"]["version"]) if frontmatter.get("metadata", {}).get("version") else None,
        license=frontmatter.get("license"),
        compatibility=frontmatter.get("compatibility"),
        allowed_tools=allowed_tools,
        metadata={k: str(v) for k, v in frontmatter.get("metadata", {}).items()},
        required_env=_string_list(requires.get("env")),
        primary_env=primary_env,
        required_bins=_string_list(requires.get("bins")),
    )


def _parsed_scripts(skill_record: dict) -> dict[str, str]:
    """Return the bundled-scripts dict (filename -> content) or empty dict."""
    scripts_json = skill_record.get("scripts_json")
    if not scripts_json:
        return {}
    if isinstance(scripts_json, str):
        return json.loads(scripts_json)
    return dict(scripts_json)


def _build_activation_response(skill_record: dict) -> str:
    """Build the full activation response for a skill tool call.

    Returns the full prompt + references content. Bundled scripts are
    loaded into the file state by the tool function itself; here we just
    note where to find them so the agent can call ``run_file``.
    """
    parsed = parse_skill_md(skill_record["skill_md"])
    parts = [parsed.prompt]

    # Append reference file contents
    refs_json = skill_record.get("references_json")
    if refs_json:
        refs = json.loads(refs_json) if isinstance(refs_json, str) else refs_json
        for filename, content in refs.items():
            parts.append(f"\n\n---\n## Reference: {filename}\n\n{content}")

    scripts = _parsed_scripts(skill_record)
    if scripts:
        prefix = f"/skills/{parsed.name}/scripts"
        paths = ", ".join(f"`{prefix}/{name}`" for name in scripts)
        parts.append(
            "\n\n---\nBundled scripts are loaded into the conversation filesystem "
            f"at: {paths}. Execute them with the `run_file` tool (e.g. "
            f"`run_file('{prefix}/{next(iter(scripts))}')`). The scripts use "
            "PEP 723 inline metadata so `uv run` resolves their dependencies."
        )

    # Declared credential names (from metadata.openclaw.requires.env). These
    # are credentials the skill's scripts MAY use. The runtime treats them as
    # optional: if the user has the credential stored, it gets injected into
    # the sandbox; if not, it's silently skipped and the script/CLI is
    # responsible for surfacing a clear error if it actually needed the
    # credential. Always pass the full list so the runtime knows which names
    # to plumb when available.
    if parsed.required_env:
        bullets = "\n".join(f"  - {n}" for n in parsed.required_env)
        parts.append(
            "\n\n---\n"
            "This skill MAY use these credentials (injected when set, "
            "skipped when not):\n"
            f"{bullets}\n"
            "Pass the full list to run_file's `credentials` argument as "
            '`[{"kind": "env_vars", "names": [...]}]` when executing the '
            "bundled scripts. The script will fail with a clear message if it "
            "needed a credential that wasn't injected — surface that message "
            "to the user; do not invent values."
        )

    return "\n".join(parts)


def has_scripts(skill_record: dict) -> bool:
    """Check if a skill record has associated scripts."""
    scripts_json = skill_record.get("scripts_json")
    if not scripts_json:
        return False
    scripts = json.loads(scripts_json) if isinstance(scripts_json, str) else scripts_json
    return bool(scripts)


# Languages in fenced code blocks that imply execution capability is needed
_EXECUTABLE_LANGS = {"bash", "sh", "shell", "python", "python3", "py", "zsh", "fish", "powershell", "ruby", "perl"}

# allowed-tools values that imply execution capability
_EXECUTION_TOOLS = {"bash", "shell", "terminal", "code_execution", "run_file", "execute_python_code"}


def requires_sandbox(skill_record: dict) -> bool:
    """Check if a skill requires sandbox access to be useful.

    A skill requires sandbox when it has:
    - Bundled scripts (scripts_json)
    - allowed-tools referencing execution tools (Bash, etc.)
    - Metadata indicating binary dependencies (openclaw requires.bins)
    - Executable code blocks in the prompt (```bash, ```python, etc.)
    """
    # Check for bundled scripts
    if has_scripts(skill_record):
        return True

    skill_md = skill_record.get("skill_md", "")
    try:
        parsed = parse_skill_md(skill_md)
    except ValueError:
        return False

    # Check allowed-tools for execution tools
    for tool in parsed.allowed_tools:
        # allowed-tools can be "Bash(python:*)" style — check the base name
        base = tool.split("(")[0].strip().lower()
        if base in _EXECUTION_TOOLS:
            return True

    # Openclaw-style binary requirements (parsed into ParsedSkill.required_bins
    # by parse_skill_md, so no second YAML pass here).
    if parsed.required_bins:
        return True

    # Check for executable code blocks in the prompt body
    code_block_pattern = re.compile(r"```(\w+)")
    for match in code_block_pattern.finditer(parsed.prompt):
        lang = match.group(1).lower()
        if lang in _EXECUTABLE_LANGS:
            return True

    return False


def create_skill_tool(skill_record: dict) -> StructuredTool:
    """Create a LangChain tool for a skill.

    Tool description = skill's short description (loaded at graph build time).
    On activation the tool returns a Command that:
    - Emits the full prompt + reference contents as a ToolMessage.
    - Loads every bundled script from ``scripts_json`` into the graph's
      ``files`` state under ``/skills/<skill-name>/scripts/<filename>`` so
      the agent can call ``run_file`` on them immediately. ``run_file`` then
      uploads the file content to the Daytona sandbox and executes via
      ``uv run``. Namespacing by skill name prevents filename collisions
      across skills (e.g. multiple fetchers each shipping a ``fetch.py``).
    """
    parsed = parse_skill_md(skill_record["skill_md"])
    skill_id = skill_record["id"]
    record = skill_record  # Capture for closure
    scripts = _parsed_scripts(record)
    script_path_prefix = f"/skills/{parsed.name}/scripts"

    async def _activate(*, runtime: ToolRuntime) -> Command:
        """Activate this skill and load any bundled scripts into file state."""
        prompt = _build_activation_response(record)
        update: dict = {"messages": [ToolMessage(content=prompt, tool_call_id=runtime.tool_call_id)]}
        if scripts:
            now = datetime.now(UTC).isoformat()
            files_update: dict = {}
            for filename, content in scripts.items():
                # Content is stored as a single string; split into lines to
                # match the shape used by write_file.
                if isinstance(content, list):
                    lines = content
                else:
                    lines = str(content).split("\n")
                files_update[f"{script_path_prefix}/{filename}"] = {
                    "content": lines,
                    "source": "skill",
                    "skill_id": skill_id,
                    "created_at": now,
                    "modified_at": now,
                }
            update["files"] = files_update
        return Command(update=update)

    tool = StructuredTool.from_function(
        coroutine=_activate,
        name=f"skill_{parsed.name.replace('-', '_')}",
        description=f"Skill: {parsed.description}",
    )
    # Stash metadata on the tool for graph integration
    tool.metadata = {  # type: ignore[assignment]
        "skill_id": skill_id,
        "requires_sandbox": requires_sandbox(skill_record),
    }
    return tool
