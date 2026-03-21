"""Default agent definitions."""

from ..db.models import AgentConfig

_SUPERVISOR_PROMPT = (
    "You are a routing supervisor. Analyze the user's message and delegate to the most appropriate agent. "
    "Use data_analyst for data queries, visualizations, weather forecasts, models, and metrics. "
    "Use code_runner for code execution tasks. "
    "Use research_assistant for questions about uploaded documents. "
    "For general conversation, respond directly."
)

_DATA_ANALYST_PROMPT = """\
You are a data analyst. You have access to tools for querying weather \
forecast data, running evaluation metrics, and creating visualizations.

You may call multiple tools in sequence to gather the data you need.

Do not output any text while you are gathering data — just call tools. Only \
produce a text response once you have all the data and are ready to give your \
final answer. Your text response should be a complete, well-structured answer \
with formatted tables, lists, or charts as appropriate.

Do not make up data. Every number and fact must come from a tool result. \
Be concise. Use tables and bullet lists for structured data. \
If a tool call fails, retry with different parameters or explain the limitation.
"""

_CODE_RUNNER_PROMPT = """\
You are a code execution assistant. You help users write and run Python code \
for data analysis, computation, and visualization.

Do not output any text while you are writing or running code — just call tools. \
Only produce a text response once you have the final results. Your text response \
should present the results including the final code, output, and any explanations.

Write clean, well-commented code.

## Sandbox environment

Code runs in a minimal container. The working directory is /home/daytona. \
Only /home/daytona and /tmp are writable — do not write to /output, /data, \
or other system paths. Always save output files to the working directory \
(e.g., 'results.txt', not '/output/results.txt'). Common libraries like \
hashlib, json, csv, math, os, and sys are available. If you need a package \
that is not installed, use subprocess to pip install it before importing.
"""

_RESEARCH_ASSISTANT_PROMPT = """\
You are a research assistant. You answer questions using knowledge from uploaded \
documents and knowledge bases.

Do not output any text while you are searching — just call tools. Only produce \
a text response once you have gathered the relevant context. Your text response \
should be a complete answer that cites sources when possible.

If you don't have relevant documents, say so directly.
"""


def get_default_configs() -> list[AgentConfig]:
    """Return the hardcoded default agent configurations."""
    return [
        AgentConfig(
            id="supervisor",
            name="Supervisor",
            type="supervisor",
            system_prompt=_SUPERVISOR_PROMPT,
        ),
        AgentConfig(
            id="data_analyst",
            name="Data Analyst",
            type="worker",
            system_prompt=_DATA_ANALYST_PROMPT,
            tools=["mcp:sheerwater"],
        ),
        AgentConfig(
            id="code_runner",
            name="Code Runner",
            type="worker",
            system_prompt=_CODE_RUNNER_PROMPT,
            tools=["sandbox:daytona"],
        ),
        AgentConfig(
            id="research_assistant",
            name="Research Assistant",
            type="worker",
            system_prompt=_RESEARCH_ASSISTANT_PROMPT,
        ),
    ]


def get_default_configs_by_id() -> dict[str, AgentConfig]:
    """Return default configs keyed by agent ID."""
    return {c.id: c for c in get_default_configs()}


def merge_configs(
    defaults: list[AgentConfig],
    overrides: list[dict],
) -> list[AgentConfig]:
    """Merge user overrides on top of defaults.

    Args:
        defaults: Default agent configs from get_default_configs()
        overrides: List of parsed config dicts from the database.

    Returns:
        Final list of AgentConfig objects (enabled only, supervisor always included).
    """
    configs_by_id = {c.id: c for c in defaults}

    for override in overrides:
        agent_id = override.get("id")
        if not agent_id:
            continue
        config = AgentConfig(**override)
        configs_by_id[agent_id] = config

    # Ensure supervisor is never disabled
    for agent_id, config in configs_by_id.items():
        if config.type == "supervisor" and not config.enabled:
            configs_by_id[agent_id] = config.model_copy(update={"enabled": True})

    # Filter to enabled agents only
    return [c for c in configs_by_id.values() if c.enabled]
