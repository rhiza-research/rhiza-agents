"""Dynamic LangGraph graph construction from AgentConfig objects."""

import hashlib
import json
import logging
from collections.abc import Sequence
from typing import Annotated, NotRequired, TypedDict

from langchain.agents import create_agent
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    ModelCallLimitMiddleware,
    ModelRetryMiddleware,
    SummarizationMiddleware,
)
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from langgraph.managed.is_last_step import RemainingStepsManager
from langgraph_supervisor import create_supervisor

from ..db.models import AgentConfig

logger = logging.getLogger(__name__)

_graph_cache: dict = {}

# Tools that require human approval before execution
_HITL_TOOLS = {"execute_python_code", "run_file"}


def _merge_files(current: dict, update: dict) -> dict:
    """Reducer that merges file updates into the existing files dict.

    Each update is a dict of path -> file_data. New entries are added,
    existing entries are replaced (last write wins).
    """
    merged = dict(current) if current else {}
    if update:
        merged.update(update)
    return merged


class AgentGraphState(TypedDict):
    """State schema for worker agents (used by create_agent)."""

    messages: Annotated[Sequence[AnyMessage], add_messages]
    files: Annotated[dict, _merge_files]


class SupervisorGraphState(TypedDict):
    """State schema for the supervisor graph (used by create_supervisor).

    create_supervisor internally uses create_react_agent which requires
    remaining_steps. create_agent rejects it. So we need separate schemas.
    """

    messages: Annotated[Sequence[AnyMessage], add_messages]
    remaining_steps: NotRequired[Annotated[int, RemainingStepsManager]]
    files: Annotated[dict, _merge_files]


def _build_worker_middleware(tools: list) -> list:
    """Build the middleware stack for a worker agent.

    Includes summarization, model retry, model call limit, and HITL
    (when the worker has sandbox tools that need approval).
    """
    middleware = [
        SummarizationMiddleware(
            model="anthropic:claude-haiku-3-20240307",
            trigger=("tokens", 100_000),
            keep=("messages", 10),
        ),
        ModelRetryMiddleware(max_retries=3),
        ModelCallLimitMiddleware(run_limit=50),
    ]

    # Add HITL middleware if any tools require approval
    tool_names = {getattr(t, "name", None) for t in tools}
    hitl_tools = tool_names & _HITL_TOOLS
    if hitl_tools:
        middleware.append(
            HumanInTheLoopMiddleware(
                interrupt_on={name: True for name in hitl_tools},
            )
        )

    return middleware


def _build_supervisor_middleware() -> list:
    """Build the middleware stack for the supervisor agent."""
    return [
        SummarizationMiddleware(
            model="anthropic:claude-haiku-3-20240307",
            trigger=("tokens", 100_000),
            keep=("messages", 10),
        ),
        ModelRetryMiddleware(max_retries=3),
        ModelCallLimitMiddleware(run_limit=50),
    ]


def _config_hash(configs: list[AgentConfig], mcp_server_ids: list[str] | None = None) -> str:
    data = json.dumps(
        {
            "configs": [c.model_dump() for c in configs],
            "mcp_servers": sorted(mcp_server_ids or []),
        },
        sort_keys=True,
    )
    return hashlib.sha256(data.encode()).hexdigest()


async def _resolve_tools(
    config: AgentConfig,
    mcp_tools: list,
    vectorstore_manager=None,
    db=None,
    mcp_tools_by_server: dict[str, list] | None = None,
) -> list:
    """Resolve tool identifiers to actual tool objects."""
    tools = []
    all_mcp = mcp_tools_by_server or {}
    for tool_id in config.tools:
        if tool_id.startswith("mcp:"):
            server_id = tool_id[4:]
            if server_id in all_mcp:
                tools.extend(all_mcp[server_id])
            else:
                logger.info("MCP server %s not loaded, skipping", server_id)
        elif tool_id == "sandbox:daytona":
            from .tools.files import run_file, write_file
            from .tools.sandbox import execute_python_code, is_sandbox_available

            # File tools are always available (write to state, not sandbox)
            tools.append(write_file)
            if is_sandbox_available():
                tools.append(execute_python_code)
                tools.append(run_file)
            # If no API key, silently skip sandbox tools -- agent works without them
        else:
            logger.info("Tool type %s not yet implemented, skipping", tool_id)

    # Resolve vectorstore_ids into retrieval tools
    if config.vectorstore_ids and vectorstore_manager and db:
        from .tools.vectordb import create_retrieval_tool

        for vs_id in config.vectorstore_ids:
            vs_record = await db.get_vectorstore_by_id(vs_id)
            if vs_record:
                tools.append(
                    create_retrieval_tool(
                        vectorstore_manager,
                        vs_record["collection_name"],
                        vs_record["display_name"],
                        vs_record.get("description", ""),
                    )
                )

    return tools


async def build_graph(
    configs: list[AgentConfig],
    mcp_tools: list,
    checkpointer,
    vectorstore_manager=None,
    db=None,
    mcp_tools_by_server: dict[str, list] | None = None,
    mcp_server_names: dict[str, str] | None = None,
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

    # Log the full graph configuration for debugging
    all_mcp = mcp_tools_by_server or {}
    logger.info(
        "Building graph: agents=%s, mcp_servers=%s (%s), vectorstore_manager=%s",
        [c.id for c in configs if c.enabled],
        list(all_mcp.keys()),
        {k: len(v) for k, v in all_mcp.items()},
        vectorstore_manager is not None,
    )

    worker_agents = []
    agent_tool_descriptions = []
    for wc in worker_configs:
        tools = await _resolve_tools(wc, mcp_tools, vectorstore_manager, db, mcp_tools_by_server)
        middleware = _build_worker_middleware(tools)
        model = ChatAnthropic(
            model=wc.model,
            max_tokens=16000,
            thinking={"type": "enabled", "budget_tokens": 10000},
        )
        worker = create_agent(
            model,
            tools,
            system_prompt=wc.system_prompt,
            middleware=middleware,
            name=wc.id,
            state_schema=AgentGraphState,
        )
        worker_agents.append(worker)

        tool_names = [getattr(t, "name", "?") for t in tools]
        logger.info(
            "Created worker: %s model=%s tools=[%s] vectorstores=%s middleware=%d",
            wc.id,
            wc.model,
            ", ".join(tool_names),
            wc.vectorstore_ids or [],
            len(middleware),
        )
        if tool_names:
            agent_tool_descriptions.append(f"- {wc.id} ({wc.name}): tools=[{', '.join(tool_names)}]")

    # Build supervisor prompt with dynamic tool info so it knows which agent has what
    supervisor_prompt = supervisor_config.system_prompt
    if agent_tool_descriptions:
        supervisor_prompt += "\n\nCurrent agent tool assignments:\n" + "\n".join(agent_tool_descriptions)

    # Include MCP server names and their tools so supervisor can answer questions about them
    if mcp_tools_by_server:
        names = mcp_server_names or {}
        mcp_info = []
        for server_id, tools in mcp_tools_by_server.items():
            display = names.get(server_id, server_id)
            tool_names = [getattr(t, "name", "?") for t in tools]
            mcp_info.append(f'- "{display}": tools=[{", ".join(tool_names)}]')
        supervisor_prompt += "\n\nConnected MCP servers:\n" + "\n".join(mcp_info)
        supervisor_prompt += (
            "\n\nIf a user asks about MCP servers or their tools,"
            " answer directly from this information. Do not delegate."
        )

    supervisor = create_supervisor(
        model=ChatAnthropic(
            model=supervisor_config.model,
            max_tokens=16000,
            thinking={"type": "enabled", "budget_tokens": 10000},
        ),
        agents=worker_agents,
        prompt=supervisor_prompt,
        state_schema=SupervisorGraphState,
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
    vectorstore_manager=None,
    db=None,
    mcp_tools_by_server: dict[str, list] | None = None,
    mcp_server_names: dict[str, str] | None = None,
):
    """Get a cached graph or build a new one."""
    h = _config_hash(configs, list((mcp_tools_by_server or {}).keys()))
    if h not in _graph_cache:
        _graph_cache[h] = await build_graph(
            configs, mcp_tools, checkpointer, vectorstore_manager, db, mcp_tools_by_server, mcp_server_names
        )
    return _graph_cache[h]


def invalidate_graph_cache(config_hash: str | None = None):
    """Invalidate cached graph. If config_hash is None, clear all."""
    if config_hash is None:
        _graph_cache.clear()
    else:
        _graph_cache.pop(config_hash, None)
