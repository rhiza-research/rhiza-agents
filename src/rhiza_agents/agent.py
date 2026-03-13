"""Deep agent definition for rhiza-agents.

Exports a factory function `graph` that LangGraph Server calls per-run.
See: https://docs.langchain.com/langsmith/agent-server
"""

import hashlib
import json
import logging
import os

from deepagents import create_deep_agent
from langchain.agents.middleware import ModelRetryMiddleware
from langchain.agents.middleware.tool_call_limit import ToolCallLimitMiddleware
from langchain_anthropic import ChatAnthropic
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from rhiza_agents.agents.registry import get_default_configs, merge_configs
from rhiza_agents.db.database import Database
from rhiza_agents.tools.mcp import get_all_mcp_tools
from rhiza_agents.tools.sandbox import execute_python_code, is_sandbox_available

logger = logging.getLogger(__name__)

# Cache compiled graphs by config hash
_graph_cache: dict[str, CompiledStateGraph] = {}

# Default system prompt for the main agent (when no config overrides)
DEFAULT_SYSTEM_PROMPT = """\
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


def _config_hash(configs: list) -> str:
    """Compute a hash of agent configs for cache lookup."""
    data = json.dumps([c.model_dump() for c in configs], sort_keys=True)
    return hashlib.sha256(data.encode()).hexdigest()


def _resolve_tools(tool_ids: list[str], mcp_tools: list, sandbox_available: bool) -> list:
    """Resolve tool ID strings to actual tool objects.

    Tool ID scheme:
    - "mcp:sheerwater" → all MCP tools from sheerwater server
    - "sandbox:daytona" → execute_python_code tool
    """
    tools = []
    for tool_id in tool_ids:
        if tool_id.startswith("mcp:"):
            tools.extend(mcp_tools)
        elif tool_id == "sandbox:daytona" and sandbox_available:
            tools.append(execute_python_code)
        else:
            logger.info("Tool %s not available, skipping", tool_id)
    return tools


async def _build_graph(config: RunnableConfig) -> CompiledStateGraph:
    """Build a deep agent graph based on user config.

    Reads user agent overrides from the shared database, merges with defaults,
    resolves tools, and creates the deep agent.
    """
    user_id = config.get("configurable", {}).get("user_id", "default")

    # Load user overrides from shared DB
    db_path = os.environ.get("CONFIG_DB_PATH", "./config.db")
    db_url = f"sqlite:///{db_path}"
    db = Database(db_url)
    await db.connect()

    try:
        # Get user's agent config overrides
        override_rows = await db.get_user_agent_configs(user_id)
        overrides = [json.loads(row["config_json"]) for row in override_rows]

        # Get additional MCP server configs
        mcp_server_configs = await db.list_mcp_servers()
    finally:
        await db.disconnect()

    # Merge defaults with user overrides
    defaults = get_default_configs()
    effective_configs = merge_configs(defaults, overrides)

    # Check cache
    config_hash = _config_hash(effective_configs)
    if config_hash in _graph_cache:
        return _graph_cache[config_hash]

    # Load MCP tools from all configured servers
    mcp_tools = await get_all_mcp_tools(mcp_server_configs)

    sandbox_available = is_sandbox_available()

    # Build subagents from effective worker configs
    subagents = []
    for agent_config in effective_configs:
        if agent_config.type != "worker":
            continue
        resolved_tools = _resolve_tools(agent_config.tools, mcp_tools, sandbox_available)
        subagents.append(
            {
                "name": agent_config.id,
                "description": agent_config.name,
                "system_prompt": agent_config.system_prompt,
                "tools": resolved_tools,
                "model": agent_config.model,
            }
        )

    # Build the deep agent
    compiled = create_deep_agent(
        model=ChatAnthropic(model="claude-sonnet-4-20250514"),
        tools=mcp_tools,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        subagents=subagents or None,
        middleware=[
            ModelRetryMiddleware(max_retries=3),
            ToolCallLimitMiddleware(run_limit=50),
        ],
    )

    _graph_cache[config_hash] = compiled
    logger.info("Built deep agent graph (hash=%s, %d subagents)", config_hash[:8], len(subagents))
    return compiled


async def graph(config: RunnableConfig) -> CompiledStateGraph:
    """Factory function for LangGraph Server.

    LangGraph Server calls this per-run with the run's RunnableConfig,
    enabling per-user graph customization based on stored agent configs.
    """
    return await _build_graph(config)
