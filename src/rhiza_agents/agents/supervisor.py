"""Supervisor convenience module tying together registry + graph."""

import json

from ..db.models import AgentConfig
from ..db.sqlite import Database
from .graph import get_or_build_graph
from .registry import get_default_configs, merge_configs


async def get_agent_graph(
    mcp_tools: list,
    checkpointer,
    user_configs: list[AgentConfig] | None = None,
    user_id: str | None = None,
    db: Database | None = None,
    vectorstore_manager=None,
    mcp_tools_by_server: dict[str, list] | None = None,
    mcp_server_names: dict[str, str] | None = None,
    skill_tools: dict | None = None,
):
    """Get the compiled agent graph.

    If user_id and db are provided, loads overrides from the database.
    If user_configs is provided directly, uses those.
    Otherwise uses defaults.
    """
    if user_id and db:
        defaults = get_default_configs()
        override_rows = await db.get_user_agent_configs(user_id)
        overrides = [json.loads(row["config_json"]) for row in override_rows]
        configs = merge_configs(defaults, overrides)
    elif user_configs:
        configs = user_configs
    else:
        configs = get_default_configs()
    return await get_or_build_graph(
        configs,
        mcp_tools,
        checkpointer,
        vectorstore_manager,
        db,
        mcp_tools_by_server,
        mcp_server_names,
        skill_tools,
    )
