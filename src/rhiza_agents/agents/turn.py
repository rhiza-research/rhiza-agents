"""Shared async-generator turn driver.

One loop drives a single agent turn for every caller (web SSE route and the
Slack connector): it builds the Langfuse trace per resume round, runs
``graph.astream`` with ``version="v2"`` dict chunks, and translates each chunk
into a typed event dict that the caller maps to its own transport.

The driver owns:
  - the auto-resume re-loop, bounded by ``max_resumes``
  - per-round Langfuse trace id + handler construction (``trace_id`` events)
  - token / thinking extraction from ``messages`` chunks
  - tool-call / tool-result dedup across the subgraph + parent duplication that
    ``subgraphs=True`` produces (``tool_start`` / ``tool_end`` / ``chart`` /
    ``files_changed`` events)
  - interrupt handling: in ``auto`` mode it resumes with an approve decision; in
    ``review`` mode it yields the raw interrupt value and stops auto-resuming

The driver does NOT enrich interrupts, touch the DB, or create/touch
conversations — callers own that. The yielded interrupt value is the raw
langgraph interrupt value; the web route enriches it before serializing.
"""

import json
import logging

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from ..messages import (
    extract_chart_url,
    extract_content_blocks,
    extract_content_blocks_from_token,
)
from ..observability import make_langfuse_handler, new_trace_id

logger = logging.getLogger(__name__)

# Bound the HITL auto-approve loop so a tool that interrupts on every round
# cannot spin forever (runaway model spend).
MAX_AUTO_RESUMES = 25

# Tools whose output carries an embedded chart URL the frontend can render.
_CHART_TOOLS = ("tool_render_plotly", "tool_generate_comparison_chart")


async def run_agent_turn(
    graph,
    *,
    thread_id: str,
    stream_input,
    execution_mode: str,
    prompt_refs: dict | None = None,
    prompt_objects: dict | None = None,
    user_id: str | None = None,
    max_resumes: int = MAX_AUTO_RESUMES,
):
    """Drive one agent turn, yielding typed event dicts.

    Yields dicts with a ``type`` key:
      - ``{"type": "trace_id", "trace_id": str}``
      - ``{"type": "thinking", "content": str}``
      - ``{"type": "token", "content": str}``
      - ``{"type": "tool_start", "name": str, "args": ...}``
      - ``{"type": "tool_end", "name": str, "output": str}``
      - ``{"type": "chart", "url": str}``
      - ``{"type": "files_changed"}``
      - ``{"type": "interrupt", "value": <raw interrupt value>}``

    ``execution_mode`` is ``"auto"`` (auto-approve interrupts and resume) or
    ``"review"`` (yield the interrupt and stop auto-resuming).

    The seen-id dedup sets persist across resume rounds so a tool surfaced in
    one round is not re-emitted when the graph resumes.
    """
    seen_tool_call_ids: set = set()
    seen_tool_result_ids: set = set()

    for _resume_round in range(max_resumes + 1):
        auto_resume = False
        trace_id = new_trace_id()
        config = {
            "configurable": {"thread_id": thread_id},
            "metadata": {
                "langfuse_session_id": thread_id,
                "rhiza_prompts": prompt_refs or {},
            },
        }
        if user_id is not None:
            config["metadata"]["langfuse_user_id"] = user_id
        lf_handler = make_langfuse_handler(trace_id=trace_id, prompt_objects=prompt_objects)
        if lf_handler:
            config["callbacks"] = [lf_handler]
            yield {"type": "trace_id", "trace_id": trace_id}

        async for chunk in graph.astream(
            stream_input,
            config=config,
            stream_mode=["messages", "updates", "custom"],
            version="v2",
            subgraphs=True,
        ):
            chunk_type = chunk["type"]

            if chunk_type == "messages":
                token, metadata = chunk["data"]
                # Only process AI model output, not tool results.
                if isinstance(token, ToolMessage):
                    continue
                if metadata.get("langgraph_node", "") == "tools":
                    continue
                text, reasoning = extract_content_blocks_from_token(token)
                if not text and not reasoning:
                    continue
                if reasoning:
                    yield {"type": "thinking", "content": reasoning}
                if text:
                    yield {"type": "token", "content": text}

            elif chunk_type == "updates":
                update_data = chunk["data"]

                # HITL interrupts appear as __interrupt__ in updates. Only handle
                # top-level (empty ns) to avoid duplicates from subgraphs.
                if "__interrupt__" in update_data:
                    if chunk.get("ns"):
                        continue
                    if execution_mode == "auto":
                        # Auto-approve: resume immediately without user interaction.
                        stream_input = Command(resume={"decisions": [{"type": "approve"}]})
                        auto_resume = True
                        break
                    for intr in update_data["__interrupt__"]:
                        intr_data = getattr(intr, "value", intr)
                        yield {"type": "interrupt", "value": intr_data}
                    continue

                # Extract tool call/result info from node updates. Deduplicate by
                # tool call ID since subgraphs=True surfaces the same event from
                # both subgraph and parent.
                for _node_name, node_data in update_data.items():
                    if not isinstance(node_data, dict):
                        continue
                    for msg in node_data.get("messages", []):
                        # Tool calls from AI messages.
                        if hasattr(msg, "tool_calls") and msg.tool_calls:
                            for tc in msg.tool_calls:
                                tc_id = tc.get("id")
                                if tc_id:
                                    if tc_id in seen_tool_call_ids:
                                        continue
                                    seen_tool_call_ids.add(tc_id)
                                yield {"type": "tool_start", "name": tc["name"], "args": tc["args"]}
                        # Tool results from ToolMessages.
                        if isinstance(msg, ToolMessage):
                            result_id = getattr(msg, "tool_call_id", None)
                            if result_id:
                                if result_id in seen_tool_result_ids:
                                    continue
                                seen_tool_result_ids.add(result_id)
                            tool_content = msg.content
                            # Extract text from content block lists.
                            if isinstance(tool_content, list):
                                text, _ = extract_content_blocks(tool_content)
                                tool_content = text or tool_content
                            if isinstance(tool_content, str):
                                try:
                                    tool_content = json.loads(tool_content)
                                except (json.JSONDecodeError, TypeError):
                                    pass
                            tool_output_str = str(tool_content)[:1000]
                            yield {"type": "tool_end", "name": msg.name, "output": tool_output_str}
                            # Emit chart event for plotly renders.
                            if msg.name in _CHART_TOOLS:
                                html_url = extract_chart_url(msg.content)
                                if html_url:
                                    yield {"type": "chart", "url": html_url}
                            # Emit files_changed after run_file so the client
                            # refetches the file list.
                            if msg.name == "run_file":
                                yield {"type": "files_changed"}

            elif chunk_type == "custom":
                custom_data = chunk["data"]
                if isinstance(custom_data, dict) and custom_data.get("type") == "files_changed":
                    yield {"type": "files_changed"}

        if not auto_resume:
            break
    else:
        logger.warning("Auto-resume cap (%d) reached for thread %s", max_resumes, thread_id)
