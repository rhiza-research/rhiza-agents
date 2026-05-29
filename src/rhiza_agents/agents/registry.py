"""Default agent definition.

The platform runs a single agent. The supervisor + multi-worker topology was
removed; one agent holds the union of the configured tools.
"""

from ..db.models import AgentConfig

# Fixed id for the single agent. Used as the agent_id key in user_agent_configs.
SINGLE_AGENT_ID = "assistant"

_SINGLE_AGENT_PROMPT = """\
You are a helpful data and research assistant. You answer questions and produce \
results — including charts and other files — by calling the tools available to \
you: weather/forecast data queries, knowledge-base search, and trusted skills.

Do not output any text while you are calling tools — just call tools. Only \
produce a text response once you have the results. Your response should be \
complete and well-structured, using tables, lists, or charts as appropriate, \
and should cite sources when answering from documents.

Do not make up data. Every number and fact must come from a tool result. If a \
tool call fails, retry with different parameters or explain the limitation. If \
you lack the data or a relevant document, say so directly.

## Running skills

Skills are trusted, installed capabilities. To run a skill, first activate it \
via its `skill_<name>` tool; the activation message tells you which script \
paths are available. Then execute a script with `run_file`, whose path must be \
of the form `/skills/<skill-name>/scripts/<filename>`. Skill scripts run with \
elevated privileges and may write output (e.g. a chart) into the \
per-conversation `/workspace`. You cannot write files yourself — to produce a \
persistent file, invoke a skill that does. If no skill exists for what you \
need, say so.

`/data` is a shared read-only cache populated by skills; `/workspace` is the \
per-conversation output area. Scripts run via `run_file` use `uv run`, which \
resolves the skill author's declared dependencies — you don't manage them.
"""


def get_single_agent_prompt() -> str:
    """Return the single agent's system prompt."""
    return _SINGLE_AGENT_PROMPT


def get_default_configs() -> list[AgentConfig]:
    """Return the default configuration: a single agent holding all tools."""
    return [
        AgentConfig(
            id=SINGLE_AGENT_ID,
            name="Assistant",
            type="worker",
            system_prompt=_SINGLE_AGENT_PROMPT,
            tools=["mcp:sheerwater", "sandbox:daytona"],
        ),
    ]


def get_default_configs_by_id() -> dict[str, AgentConfig]:
    """Return default configs keyed by agent ID."""
    return {c.id: c for c in get_default_configs()}


def merge_configs(
    defaults: list[AgentConfig],
    overrides: list[dict],
) -> list[AgentConfig]:
    """Overlay user overrides on top of the defaults, keyed by agent id.

    Args:
        defaults: Default agent configs from get_default_configs().
        overrides: List of parsed config dicts from the database.

    Returns:
        The effective agent configs.
    """
    configs_by_id = {c.id: c for c in defaults}
    for override in overrides:
        agent_id = override.get("id")
        # Ignore overrides for unknown ids — e.g. stale per-worker rows
        # (data_analyst/code_runner/...) left in user_agent_configs from the
        # pre-collapse multi-agent topology.
        if not agent_id or agent_id not in configs_by_id:
            continue
        configs_by_id[agent_id] = AgentConfig(**override)
    return list(configs_by_id.values())
