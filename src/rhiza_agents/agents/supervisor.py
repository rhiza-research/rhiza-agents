"""Supervisor convenience module tying together registry + graph."""

from ..db.models import AgentConfig
from .graph import get_or_build_graph
from .registry import get_default_configs


async def get_agent_graph(
    mcp_tools: list,
    checkpointer,
    user_configs: list[AgentConfig] | None = None,
):
    """Get the compiled agent graph.

    If user_configs is provided, use those. Otherwise use defaults.
    Config merging (user overrides on top of defaults) is handled by
    the caller -- this function just takes the final config list.
    """
    configs = user_configs or get_default_configs()
    return await get_or_build_graph(configs, mcp_tools, checkpointer)
