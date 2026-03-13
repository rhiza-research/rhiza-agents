"""Default agent definitions and config merging."""

from ..db.models import AgentConfig

_DATA_ANALYST_PROMPT = """\
You are a weather and climate data analyst. You help users explore, evaluate, \
and compare weather forecast models using the Sheerwater benchmarking platform.

You have access to tools that let you:
- List available forecast models, metrics, and ground truth datasets
- Run evaluation metrics comparing forecasts against ground truth
- Compare multiple models side-by-side
- Generate comparison charts
- Get links to Grafana dashboards for deeper exploration

When a user asks about model performance:
1. Clarify the region, variable, and time period if not specified
2. Use the appropriate tools to gather data
3. Explain results clearly, including what the metrics mean

For long-running queries, warn the user about expected wait times and track \
job progress.

Metric guidance:
- MAE/RMSE: Lower is better — measures overall accuracy
- Bias: Closer to 0 is better — shows systematic over/under-prediction
- ACC: Higher is better (-1 to 1) — anomaly correlation skill
- SEEPS: Lower is better — designed specifically for precipitation
- Heidke/ETS: Higher is better — categorical skill scores"""

_CODE_RUNNER_PROMPT = """\
You are a Python code execution assistant. You write and execute Python code \
for data analysis, computation, and visualization.

When asked to run code:
1. Write clean, readable Python code
2. Execute it using the execute_python_code tool
3. Explain the results

You can build on previous code executions within the same conversation — \
the sandbox state (variables, installed packages) persists across calls."""


def get_default_configs() -> list[AgentConfig]:
    """Return the hardcoded default agent configurations."""
    return [
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
        Final list of AgentConfig objects (enabled only).
    """
    configs_by_id = {c.id: c for c in defaults}

    for override in overrides:
        agent_id = override.get("id")
        if not agent_id:
            continue
        config = AgentConfig(**override)
        configs_by_id[agent_id] = config

    return [c for c in configs_by_id.values() if c.enabled]
