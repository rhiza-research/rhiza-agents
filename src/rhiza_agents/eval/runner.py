"""CLI entrypoint for running a Langfuse dataset experiment.

Builds the rhiza-agents supervisor graph from default agent configs (no
per-user overrides), then uses Langfuse's `dataset.run_experiment` to invoke
the graph against every dataset item, automatically creating a dataset run
in Langfuse with linked traces and any evaluator scores.

Each item runs in an isolated in-memory checkpointer thread so items don't
share conversation state. The runner does not load vectorstores or per-user
skills — it is intentionally minimal so eval results stay reproducible.

Usage (recommended — runs inside the rhiza-agents container so all
networking and env vars are already in place):

    podman exec rhiza-agents-rhiza-agents-1 \\
        uv run python -m rhiza_agents.eval.runner \\
        --dataset weather-regression \\
        --label opus-4.6-baseline
"""

import argparse
import asyncio
import logging
import os

from langchain_core.messages import HumanMessage
from langfuse import Evaluation, get_client
from langgraph.checkpoint.memory import InMemorySaver

from ..agents.graph import build_graph
from ..agents.registry import get_default_configs
from ..agents.tools.mcp import load_mcp_tools_for_server

logger = logging.getLogger(__name__)


async def _build_eval_graph():
    """Build a supervisor graph wired with default configs and system MCP tools.

    Uses an in-memory checkpointer so eval runs never touch the dev DB.
    """
    mcp_url = os.environ.get("MCP_SERVER_URL", "")
    mcp_tools: list = []
    mcp_tools_by_server: dict[str, list] = {}
    mcp_server_names: dict[str, str] = {}
    if mcp_url:
        mcp_tools = await load_mcp_tools_for_server(mcp_url, "sse")
        if mcp_tools:
            mcp_tools_by_server = {"sheerwater": mcp_tools}
            mcp_server_names = {"sheerwater": "Sheerwater"}

    return await build_graph(
        configs=get_default_configs(),
        mcp_tools=mcp_tools,
        checkpointer=InMemorySaver(),
        mcp_tools_by_server=mcp_tools_by_server,
        mcp_server_names=mcp_server_names,
    )


def _extract_final_text(messages: list) -> str:
    """Pull the last assistant text out of a langgraph state messages list."""
    for msg in reversed(messages):
        if getattr(msg, "type", "") != "ai":
            continue
        content = getattr(msg, "content", "")
        if isinstance(content, list):
            parts = [
                block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
            ]
            joined = "".join(parts).strip()
            if joined:
                return joined
        elif isinstance(content, str) and content.strip():
            return content
    return ""


def _has_output_evaluator(*, output, **kwargs):
    """Trivial built-in evaluator: did the graph produce any text at all?

    Real evaluators (LLM-as-judge, semantic similarity, etc.) belong in
    Langfuse online evaluators or as additional functions passed here.
    Langfuse passes `input`, `expected_output`, and `metadata` via kwargs;
    this evaluator only needs `output`.
    """
    has_text = bool(output and str(output).strip())
    return Evaluation(
        name="has_output",
        value=1.0 if has_text else 0.0,
        comment="Output is non-empty" if has_text else "Output is empty",
    )


async def _run(dataset_name: str, run_name: str | None, max_concurrency: int) -> None:
    if not os.environ.get("LANGFUSE_PUBLIC_KEY"):
        raise SystemExit("LANGFUSE_PUBLIC_KEY is not set; cannot run experiment.")

    graph = await _build_eval_graph()
    client = get_client()
    dataset = client.get_dataset(dataset_name)
    logger.info("Loaded dataset %s with %d items", dataset_name, len(dataset.items))

    async def task(*, item, **_kwargs):
        # Use a unique thread id per item so items never share checkpoint state.
        thread_id = f"eval-{getattr(item, 'id', id(item))}"
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content=str(item.input))]},
            config={"configurable": {"thread_id": thread_id}},
        )
        return _extract_final_text(result.get("messages", []))

    # Default to sequential execution. The downstream MCP servers (sheerwater
    # in particular) and Anthropic rate limits both behave poorly when many
    # agent sessions run concurrently against them, and small bootstrap
    # datasets have no need for parallelism. Bump --concurrency for larger
    # datasets if the downstream services can take it.
    experiment = dataset.run_experiment(
        name="rhiza-agents-supervisor",
        run_name=run_name,
        task=task,
        evaluators=[_has_output_evaluator],
        max_concurrency=max_concurrency,
    )

    item_results = getattr(experiment, "item_results", None) or []
    logger.info(
        "Experiment finished: dataset=%s run=%s items=%d",
        dataset_name,
        run_name or "auto",
        len(item_results),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Langfuse dataset experiment against the supervisor graph.")
    parser.add_argument("--dataset", required=True, help="Langfuse dataset name")
    parser.add_argument(
        "--label",
        help="Run label (e.g. 'opus-4.6-baseline'); shown in Langfuse for comparison across runs.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help=(
            "Max parallel item executions. Defaults to 1 (sequential) so "
            "downstream MCP servers and rate limits don't get overwhelmed."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    asyncio.run(_run(args.dataset, args.label, args.concurrency))


if __name__ == "__main__":
    main()
