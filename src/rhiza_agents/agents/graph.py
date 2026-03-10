"""Dynamic LangGraph graph construction from AgentConfig objects."""

import hashlib
import json
import logging

from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent
from langgraph_supervisor import create_supervisor

from ..db.models import AgentConfig

logger = logging.getLogger(__name__)

_graph_cache: dict = {}


def _config_hash(configs: list[AgentConfig]) -> str:
    data = json.dumps([c.model_dump() for c in configs], sort_keys=True)
    return hashlib.sha256(data.encode()).hexdigest()


def _resolve_tools(config: AgentConfig, mcp_tools: list) -> list:
    """Resolve tool identifiers to actual tool objects."""
    tools = []
    for tool_id in config.tools:
        if tool_id == "mcp:sheerwater":
            tools.extend(mcp_tools)
        else:
            logger.info("Tool type %s not yet implemented, skipping", tool_id)
    return tools


async def build_graph(
    configs: list[AgentConfig],
    mcp_tools: list,
    checkpointer,
):
    """Build a compiled LangGraph StateGraph from AgentConfig objects."""
    supervisor_config = None
    worker_configs = []

    for c in configs:
        if not c.enabled:
            continue
        if c.type == "supervisor":
            supervisor_config = c
        else:
            worker_configs.append(c)

    if not supervisor_config:
        raise ValueError("No supervisor config found")

    worker_agents = []
    for wc in worker_configs:
        tools = _resolve_tools(wc, mcp_tools)
        model = ChatAnthropic(model=wc.model)
        worker = create_react_agent(model, tools, prompt=wc.system_prompt, name=wc.id)
        worker_agents.append(worker)
        logger.info("Created worker agent: %s (%d tools)", wc.id, len(tools))

    supervisor = create_supervisor(
        model=ChatAnthropic(model=supervisor_config.model),
        agents=worker_agents,
        prompt=supervisor_config.system_prompt,
        output_mode="full_history",
        add_handoff_back_messages=True,
    )

    compiled = supervisor.compile(checkpointer=checkpointer)
    logger.info("Compiled supervisor graph with %d workers", len(worker_agents))
    return compiled


async def get_or_build_graph(
    configs: list[AgentConfig],
    mcp_tools: list,
    checkpointer,
):
    """Get a cached graph or build a new one."""
    h = _config_hash(configs)
    if h not in _graph_cache:
        _graph_cache[h] = await build_graph(configs, mcp_tools, checkpointer)
    return _graph_cache[h]


def invalidate_graph_cache(config_hash: str | None = None):
    """Invalidate cached graph. If config_hash is None, clear all."""
    if config_hash is None:
        _graph_cache.clear()
    else:
        _graph_cache.pop(config_hash, None)
