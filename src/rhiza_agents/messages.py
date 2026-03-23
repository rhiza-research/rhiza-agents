"""Message processing and agent name resolution.

This module is the single source of truth for converting raw LangGraph messages
into structured output, used by both the streaming path and the message loading path.
"""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from .db.models import AgentConfig

_HANDOFF_BACK_KEY = "__is_handoff_back"
_TRANSFER_PREFIX = "transfer_to_"


def extract_chart_url(content) -> str | None:
    """Try to extract html_url from tool result content in any format.

    MCP tool results may have JSON followed by a description on a new line,
    so we try parsing just the first line if full parse fails.
    """
    if isinstance(content, dict):
        return content.get("html_url")
    if isinstance(content, str):
        # Try full string first, then first line (MCP appends description)
        for text in [content, content.split("\n")[0]]:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and "html_url" in parsed:
                    return parsed["html_url"]
            except (json.JSONDecodeError, TypeError):
                continue
    if isinstance(content, list):
        # Content block list — extract text, then parse as JSON
        text, _ = extract_content_blocks(content)
        if text:
            return extract_chart_url(text)
    return None


def extract_content_blocks(content) -> tuple[str, str]:
    """Extract text and reasoning from AIMessage content.

    Returns (text, reasoning) where each is a concatenation of the
    respective content blocks. If content is a plain string, it's
    returned as text with empty reasoning.

    Handles both LangChain-normalized types ("reasoning") and
    Anthropic raw types ("thinking").
    """
    if isinstance(content, list):
        text_parts = []
        reasoning_parts = []
        for block in content:
            if isinstance(block, dict):
                block_type = block.get("type", "")
                if block_type == "text":
                    text_parts.append(block.get("text", ""))
                elif block_type in ("reasoning", "thinking"):
                    reasoning_parts.append(block.get("reasoning") or block.get("thinking") or "")
            elif hasattr(block, "type"):
                block_type = block.type
                if block_type == "text":
                    text_parts.append(getattr(block, "text", ""))
                elif block_type in ("reasoning", "thinking"):
                    reasoning_parts.append(getattr(block, "reasoning", "") or getattr(block, "thinking", "") or "")
        return "\n".join(filter(None, text_parts)), "\n".join(filter(None, reasoning_parts))
    return (content or "").strip(), ""


def extract_content_blocks_from_token(token) -> tuple[str, str]:
    """Extract text and reasoning from a streaming token.

    Prefers content_blocks (LangChain-normalized) over raw content.
    """
    if hasattr(token, "content_blocks") and token.content_blocks:
        return extract_content_blocks(token.content_blocks)
    if hasattr(token, "content"):
        return extract_content_blocks(token.content)
    return "", ""


def build_name_mappings(configs: list[AgentConfig], mcp_tools: list) -> tuple[dict[str, str], dict[str, str]]:
    """Build agent_names and tool_to_agent mappings from a config list."""
    agent_names = {c.id: c.name for c in configs}
    tool_to_agent = {}
    for c in configs:
        for tool_id in c.tools:
            if tool_id.startswith("mcp:"):
                for t in mcp_tools:
                    tool_to_agent[t.name] = c.id
        if "sandbox:daytona" in c.tools:
            tool_to_agent["execute_python_code"] = c.id
            tool_to_agent["write_file"] = c.id
            tool_to_agent["run_file"] = c.id
    return agent_names, tool_to_agent


def resolve_agent_name(
    agent_names: dict[str, str],
    node_name: str | None = None,
    ns: list | tuple = (),
    fallback: str | None = None,
) -> str | None:
    """Resolve an agent display name from a node name or namespace.

    This is the single source of truth for agent name resolution, used by both
    the streaming path and the message loading path.

    Args:
        agent_names: mapping of agent_id -> display_name
        node_name: LangGraph node name (e.g. "research_assistant", "agent")
        ns: subgraph namespace tuple/list (e.g. ("research_assistant:uuid",))
        fallback: fallback display name if nothing resolves

    Returns:
        Resolved display name, or fallback
    """
    # Try namespace first — strip UUID suffixes
    # e.g. "research_assistant:76a5e5c1-..." -> "research_assistant"
    for ns_part in ns:
        bare = ns_part.split(":")[0] if ":" in str(ns_part) else str(ns_part)
        if bare in agent_names:
            return agent_names[bare]

    # Try node name directly
    if node_name and node_name in agent_names:
        return agent_names[node_name]

    return fallback


def process_messages(raw_messages, agent_names: dict[str, str]) -> list[dict]:
    """Process raw LangGraph messages into a single ordered list.

    Each item has a "type" field: "human", "ai", "thinking", "tool_call", "tool_result".
    AI responses include "agent_name" when known. Handoff messages are filtered out.
    """
    messages = []
    current_agent = None  # track which worker agent is active

    for msg in raw_messages:
        if isinstance(msg, HumanMessage):
            current_agent = None
            messages.append({"type": "human", "content": msg.content})

        elif isinstance(msg, AIMessage):
            # Skip handoff-back messages
            if msg.response_metadata.get(_HANDOFF_BACK_KEY, False):
                continue

            text, reasoning = extract_content_blocks(msg.content)
            tool_calls = msg.tool_calls or []

            agent_name = resolve_agent_name(agent_names, node_name=msg.name, fallback=agent_names.get(current_agent))

            # Track current agent from explicit transfers only.
            for tc in tool_calls:
                if tc["name"].startswith(_TRANSFER_PREFIX):
                    agent_id = tc["name"][len(_TRANSFER_PREFIX) :]
                    if agent_id in agent_names:
                        current_agent = agent_id

            if reasoning:
                messages.append({"type": "thinking", "content": reasoning})

            if text:
                entry = {"type": "ai", "content": text}
                if agent_name:
                    entry["agent_name"] = agent_name
                messages.append(entry)

            for tc in tool_calls:
                if tc["name"].startswith(("transfer_to_", "transfer_back_to_")):
                    continue
                messages.append({"type": "tool_call", "name": tc["name"], "args": tc["args"]})

        elif isinstance(msg, ToolMessage):
            # Skip handoff tool messages
            if msg.response_metadata.get(_HANDOFF_BACK_KEY, False):
                continue
            content = msg.content
            # Extract text from content block lists
            if isinstance(content, list):
                text, _ = extract_content_blocks(content)
                content = text or content
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    pass
            messages.append({"type": "tool_result", "name": msg.name, "content": content})
            # Extract chart URL from plotly tool results
            if msg.name in ("tool_render_plotly", "tool_generate_comparison_chart"):
                html_url = extract_chart_url(content)
                if html_url:
                    messages.append({"type": "chart", "url": html_url})

    return messages
