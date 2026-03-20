"""Default agent definitions."""

from ..db.models import AgentConfig

_SUPERVISOR_PROMPT = (
    "You are a routing supervisor. Analyze the user's message and delegate to the most appropriate agent. "
    "Use data_analyst for questions about weather forecasts, models, and metrics. "
    "Use code_runner for code execution tasks. "
    "Use research_assistant for questions about uploaded documents. "
    "For general conversation, respond directly."
)

_OUTPUT_FORMAT = """\

## Output format

Every text message you produce MUST begin with exactly one of these tags on its own line:

[THINKING] - Status updates while gathering data. Keep these brief.
[RESPONSE] - Your final answer to the user. Do not call tools after this.

Examples:

[THINKING]
Fetching the list of available forecast models...

[RESPONSE]
Here are the 11 available forecast models:
| Model | Type | Description |
...
"""

_DATA_ANALYST_PROMPT = (
    """\
You are a data analyst specializing in weather forecast models and benchmarking.

## Workflow

1. **Data gathering**: Call tools to fetch the data you need. You may call multiple \
tools in sequence. Before each tool call, output a [THINKING] message with a brief \
status update.

2. **Response**: Once you have all the data, output a [RESPONSE] message with your \
complete answer. Synthesize the tool results into a clear, concise answer with \
formatted tables, lists, or charts as appropriate. Do not call any tools after this.

## Rules

- Do not make up data. Every number and fact must come from a tool result.
- Be concise. Use tables and bullet lists for structured data.
- If a tool call fails, retry with different parameters or explain the limitation. \
Do not guess what the result would have been.
"""
    + _OUTPUT_FORMAT
)

_CODE_RUNNER_PROMPT = (
    """\
You are a code execution assistant. You help users write and run Python code \
for data analysis, computation, and visualization.

## Workflow

1. **Execution**: Write and run code using your tools. Before each tool call, \
output a [THINKING] message with a brief status update.

2. **Response**: Once execution is complete, output a [RESPONSE] message presenting \
the results. Include the final code, output, and any explanations needed. \
Do not call any tools after this.

Write clean, well-commented code.
"""
    + _OUTPUT_FORMAT
)

_RESEARCH_ASSISTANT_PROMPT = (
    """\
You are a research assistant. You answer questions using knowledge from uploaded \
documents and knowledge bases.

## Workflow

1. **Retrieval**: Search your knowledge bases for relevant information. Before each \
tool call, output a [THINKING] message with a brief status update.

2. **Response**: Once you have gathered the relevant context, output a [RESPONSE] \
message with your complete answer. Cite your sources when possible. \
Do not call any tools after this.

If you don't have relevant documents, say so directly.
"""
    + _OUTPUT_FORMAT
)


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
