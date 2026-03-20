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


def _config_hash(configs: list[AgentConfig]) -> str:
    data = json.dumps([c.model_dump() for c in configs], sort_keys=True)
    return hashlib.sha256(data.encode()).hexdigest()


async def _resolve_tools(config: AgentConfig, mcp_tools: list, vectorstore_manager=None, db=None) -> list:
    """Resolve tool identifiers to actual tool objects."""
    tools = []
    for tool_id in config.tools:
        if tool_id == "mcp:sheerwater":
            tools.extend(mcp_tools)
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
        tools = await _resolve_tools(wc, mcp_tools, vectorstore_manager, db)
        middleware = _build_worker_middleware(tools)
        model = ChatAnthropic(model=wc.model)
        worker = create_agent(
            model,
            tools,
            system_prompt=wc.system_prompt,
            middleware=middleware,
            name=wc.id,
            state_schema=AgentGraphState,
        )
        worker_agents.append(worker)
        logger.info("Created worker agent: %s (%d tools, %d middleware)", wc.id, len(tools), len(middleware))

    supervisor = create_supervisor(
        model=ChatAnthropic(model=supervisor_config.model),
        agents=worker_agents,
        prompt=supervisor_config.system_prompt,
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
):
    """Get a cached graph or build a new one."""
    h = _config_hash(configs)
    if h not in _graph_cache:
        _graph_cache[h] = await build_graph(configs, mcp_tools, checkpointer, vectorstore_manager, db)
    return _graph_cache[h]


def invalidate_graph_cache(config_hash: str | None = None):
    """Invalidate cached graph. If config_hash is None, clear all."""
    if config_hash is None:
        _graph_cache.clear()
    else:
        _graph_cache.pop(config_hash, None)
