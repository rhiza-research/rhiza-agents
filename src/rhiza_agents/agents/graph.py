"""Dynamic LangGraph graph construction from AgentConfig objects."""

import hashlib
import json
import logging
from collections.abc import Sequence
from typing import Annotated, TypedDict

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
    """State schema for the agent (used by create_agent)."""

    messages: Annotated[Sequence[AnyMessage], add_messages]
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


async def build_agent_graph(
    configs: list[AgentConfig],
    mcp_tools: list,
    checkpointer,
    vectorstore_manager=None,
    db=None,
    mcp_tools_by_server: dict[str, list] | None = None,
    skill_tools: dict | None = None,
    user_id: str | None = None,
    skills_only: bool = False,
):
    """Build a compiled single-agent graph from AgentConfig objects.

    One agent holds the union of every enabled config's resolved tools,
    deduplicated by tool name. When ``skills_only=True`` the ad-hoc
    ``execute_python_code`` tool is dropped while ``run_file`` (skill-script
    execution) is kept — this is the Slack path, where HITL interrupts on
    ``run_file`` are auto-approved and curated skills are the trust boundary.
    When ``skills_only=False`` (the web path) ``execute_python_code`` is kept
    and its HITL approval gate still fires.
    """
    from .registry import get_single_agent_prompt

    union: dict[str, object] = {}
    model_name: str | None = None
    for c in configs:
        if not c.enabled:
            continue
        if model_name is None:
            model_name = c.model
        tools = await _resolve_tools(c, mcp_tools, vectorstore_manager, db, mcp_tools_by_server, skill_tools)
        for t in tools:
            name = getattr(t, "name", None)
            if skills_only and name == "execute_python_code":
                continue
            if name and name not in union:
                union[name] = t

    tool_list = list(union.values())
    if model_name is None:
        raise ValueError("No enabled agent configuration; cannot build a graph")
    if not tool_list:
        logger.warning("Building agent graph with no tools (MCP unloaded or sandbox unavailable)")

    # Tell the agent which credential names exist (values never shown) when it
    # has a tool that consumes them (run_file or execute_python_code).
    prompt = get_single_agent_prompt()
    has_credential_tool = any(getattr(t, "name", None) in ("run_file", "execute_python_code") for t in tool_list)
    if user_id and db is not None and has_credential_tool:
        try:
            credential_names = await db.list_credential_names(user_id)
        except Exception:  # pragma: no cover - DB errors logged elsewhere
            credential_names = []
        if credential_names:
            prompt += (
                "\n\nAvailable credential names (values are never visible to you):\n"
                + "\n".join(f"  - {n}" for n in credential_names)
                + "\n\nWhen running a skill or script that needs a secret, reference these"
                " names in the `credentials` argument. Never print, log, or echo the values."
            )

    model = ChatAnthropic(
        model=model_name,
        max_tokens=16000,
        thinking={"type": "enabled", "budget_tokens": 10000},
    )
    agent = create_agent(
        model,
        tool_list,
        system_prompt=prompt,
        middleware=_build_worker_middleware(tool_list),
        name="assistant",
        state_schema=AgentGraphState,
        checkpointer=checkpointer,
    )
    logger.info(
        "Compiled agent graph: %d tools (skills_only=%s) [%s]",
        len(tool_list),
        skills_only,
        ", ".join(sorted(union.keys())),
    )
    return agent


async def get_or_build_agent_graph(
    configs: list[AgentConfig],
    mcp_tools: list,
    checkpointer,
    vectorstore_manager=None,
    db=None,
    mcp_tools_by_server: dict[str, list] | None = None,
    skill_tools: dict | None = None,
    user_id: str | None = None,
    skills_only: bool = False,
):
    """Get a cached graph or build a new one.

    The cache key includes ``skills_only`` so the web (``False``) and Slack
    (``True``) graphs for the same user never collide. It also includes the
    user_id and the user's current set of credential names so that
    adding/removing a credential transparently rebuilds the affected graph
    (the new credential name needs to appear in the system prompt).
    """
    credential_names: list[str] = []
    if user_id and db is not None:
        try:
            credential_names = await db.list_credential_names(user_id)
        except Exception:  # pragma: no cover
            credential_names = []

    h = f"{skills_only}:" + _config_hash(
        configs,
        list((mcp_tools_by_server or {}).keys()),
        list((skill_tools or {}).keys()),
        user_id=user_id,
        credential_names=credential_names,
    )
    if h not in _graph_cache:
        _graph_cache[h] = await build_agent_graph(
            configs,
            mcp_tools,
            checkpointer,
            vectorstore_manager,
            db,
            mcp_tools_by_server,
            skill_tools,
            user_id=user_id,
            skills_only=skills_only,
        )
    return _graph_cache[h]


def invalidate_graph_cache(config_hash: str | None = None):
    """Invalidate cached graph. If config_hash is None, clear all.

    The build path is keyed by ``skills_only``, so a bare config hash is
    stored under both the ``True:`` (Slack) and ``False:`` (web) prefixes.
    Pop both so a config change evicts both graphs for the user.
    """
    if config_hash is None:
        _graph_cache.clear()
    else:
        _graph_cache.pop(f"True:{config_hash}", None)
        _graph_cache.pop(f"False:{config_hash}", None)
