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


def _config_hash(
    configs: list[AgentConfig],
    mcp_server_ids: list[str] | None = None,
    skill_ids: list[str] | None = None,
    user_id: str | None = None,
    credential_names: list[str] | None = None,
) -> str:
    data = json.dumps(
        {
            "configs": [c.model_dump() for c in configs],
            "mcp_servers": sorted(mcp_server_ids or []),
            "skills": sorted(skill_ids or []),
            "user_id": user_id,
            "credentials": sorted(credential_names or []),
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
    skill_tools: dict | None = None,
) -> list:
    """Resolve tool identifiers to actual tool objects."""
    tools = []
    has_sandbox = "sandbox:daytona" in config.tools
    all_mcp = mcp_tools_by_server or {}
    all_skills = skill_tools or {}
    for tool_id in config.tools:
        if tool_id.startswith("mcp:"):
            server_id = tool_id[4:]
            if server_id in all_mcp:
                tools.extend(all_mcp[server_id])
            else:
                logger.info("MCP server %s not loaded, skipping", server_id)
        elif tool_id.startswith("skill:"):
            skill_id = tool_id[6:]
            if skill_id in all_skills:
                skill_tool = all_skills[skill_id]
                # Skills requiring execution need sandbox access
                tool_meta = getattr(skill_tool, "metadata", {}) or {}
                if tool_meta.get("requires_sandbox") and not has_sandbox:
                    logger.info("Skill %s requires sandbox, agent %s lacks it, skipping", skill_id, config.id)
                else:
                    tools.append(skill_tool)
            else:
                logger.info("Skill %s not loaded, skipping", skill_id)
        elif tool_id == "sandbox:daytona":
            from .tools.files import make_run_file
            from .tools.sandbox import is_sandbox_available, make_execute_python_code

            if is_sandbox_available():
                tools.append(make_execute_python_code(db=db))
                tools.append(make_run_file(db=db))
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
    skill_tools: dict | None = None,
    user_id: str | None = None,
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

    # Load the user's available credential names so each worker that has
    # the sandbox tool can be told what's available. Names only — never
    # values. The list is captured at graph build time; the graph cache
    # already invalidates when configs change, and the credentials route
    # invalidates when secrets are added/removed.
    credential_names: list[str] = []
    if user_id and db is not None:
        try:
            credential_names = await db.list_credential_names(user_id)
        except Exception:  # pragma: no cover - DB errors logged elsewhere
            logger.warning("Failed to load credential names for user %s", user_id, exc_info=True)

    worker_agents = []
    agent_tool_descriptions = []
    for wc in worker_configs:
        tools = await _resolve_tools(wc, mcp_tools, vectorstore_manager, db, mcp_tools_by_server, skill_tools)
        middleware = _build_worker_middleware(tools)
        model = ChatAnthropic(
            model=wc.model,
            max_tokens=16000,
            thinking={"type": "enabled", "budget_tokens": 10000},
        )

        # Augment the worker's system prompt with available credential
        # names if it has the sandbox tool. The LLM uses these names to
        # populate the `credentials` argument of execute_python_code.
        worker_prompt = wc.system_prompt
        has_sandbox_tool = any(getattr(t, "name", None) == "execute_python_code" for t in tools)
        if has_sandbox_tool and credential_names:
            worker_prompt += (
                "\n\nAvailable credential names (values are never visible to you):\n"
                + "\n".join(f"  - {n}" for n in credential_names)
                + "\n\nWhen calling execute_python_code, reference these names in the"
                " `credentials` argument to make values available to the script. Never"
                " print, log, echo, or otherwise expose credential values."
            )

        worker = create_agent(
            model,
            tools,
            system_prompt=worker_prompt,
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

    # Include available skills info so supervisor knows about skill capabilities
    if skill_tools:
        skill_info = []
        for skill_id, tool in skill_tools.items():
            desc = tool.description.removeprefix("Skill: ") if tool.description else ""
            # Find which agents have this skill assigned
            assigned_agents = [wc.name for wc in worker_configs if f"skill:{skill_id}" in wc.tools]
            if assigned_agents:
                agents_str = ", ".join(assigned_agents)
                skill_info.append(f"- {tool.name} (assigned to {agents_str}): {desc}")
        if skill_info:
            supervisor_prompt += "\n\nAvailable skills:\n" + "\n".join(skill_info)

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
    skill_tools: dict | None = None,
    user_id: str | None = None,
):
    """Get a cached graph or build a new one.

    The cache key includes the user_id and the user's current set of
    credential names so that adding/removing a credential transparently
    rebuilds the affected user's graph (the new credential name needs to
    appear in the worker system prompt).
    """
    credential_names: list[str] = []
    if user_id and db is not None:
        try:
            credential_names = await db.list_credential_names(user_id)
        except Exception:  # pragma: no cover
            credential_names = []

    h = _config_hash(
        configs,
        list((mcp_tools_by_server or {}).keys()),
        list((skill_tools or {}).keys()),
        user_id=user_id,
        credential_names=credential_names,
    )
    if h not in _graph_cache:
        _graph_cache[h] = await build_graph(
            configs,
            mcp_tools,
            checkpointer,
            vectorstore_manager,
            db,
            mcp_tools_by_server,
            mcp_server_names,
            skill_tools,
            user_id=user_id,
        )
    return _graph_cache[h]


def invalidate_graph_cache(config_hash: str | None = None):
    """Invalidate cached graph. If config_hash is None, clear all."""
    if config_hash is None:
        _graph_cache.clear()
    else:
        _graph_cache.pop(config_hash, None)
