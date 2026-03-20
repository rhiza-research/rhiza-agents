"""Dynamic LangGraph graph construction from AgentConfig objects."""

import hashlib
import json
import logging

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage
from langchain_core.messages.utils import count_tokens_approximately, trim_messages
from langgraph.prebuilt import create_react_agent
from langgraph_supervisor import create_supervisor

from ..db.models import AgentConfig

logger = logging.getLogger(__name__)

_graph_cache: dict = {}

# Max tokens for message history sent to the LLM. 100k leaves headroom
# within Claude's 200k context window for the system prompt, tool
# definitions, and the model's response.
_TRIM_MAX_TOKENS = 100_000


def _make_prompt_with_trimming(system_prompt: str, max_tokens: int = _TRIM_MAX_TOKENS):
    """Create a prompt callable that prepends the system prompt and trims messages.

    The callable receives the full graph state (dict with "messages" key)
    and returns a list of messages suitable for the LLM.
    """

    def prompt(state: dict) -> list:
        messages = state.get("messages", [])
        trimmed = trim_messages(
            messages,
            strategy="last",
            token_counter=count_tokens_approximately,
            max_tokens=max_tokens,
            start_on="human",
            end_on=("human", "tool"),
            include_system=False,
        )
        return [SystemMessage(content=system_prompt)] + trimmed

    return prompt


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
            from .tools.sandbox import execute_python_code, is_sandbox_available

            if is_sandbox_available():
                tools.append(execute_python_code)
            # If no API key, silently skip -- agent works without tools
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
        # Don't use .with_retry() here — create_react_agent needs the raw
        # ChatModel to call .bind_tools(). Retry wrapping produces a
        # RunnableRetry which lacks that method.
        model = ChatAnthropic(model=wc.model, max_retries=3)
        worker = create_react_agent(model, tools, prompt=_make_prompt_with_trimming(wc.system_prompt), name=wc.id)
        worker_agents.append(worker)
        logger.info("Created worker agent: %s (%d tools)", wc.id, len(tools))

    supervisor = create_supervisor(
        model=ChatAnthropic(model=supervisor_config.model, max_retries=3),
        agents=worker_agents,
        prompt=_make_prompt_with_trimming(supervisor_config.system_prompt),
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
