"""Message processing.

This module is the single source of truth for converting raw LangGraph messages
into structured output, used by both the streaming path and the message loading path.
"""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


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


# Internal handoff/transfer artifacts emitted by the former supervisor topology;
# filtered out when replaying historical (pre-collapse) conversations.
_HANDOFF_PREFIXES = ("transfer_to_", "transfer_back_to_")


def process_messages(raw_messages) -> list[dict]:
    """Process raw LangGraph messages into a single ordered list.

    Each item has a "type" field: "human", "ai", "thinking", "tool_call", "tool_result".
    """
    messages = []

    for msg in raw_messages:
        if isinstance(msg, HumanMessage):
            messages.append({"type": "human", "content": msg.content})

        elif isinstance(msg, AIMessage):
            text, reasoning = extract_content_blocks(msg.content)
            tool_calls = [tc for tc in (msg.tool_calls or []) if not tc.get("name", "").startswith(_HANDOFF_PREFIXES)]

            if reasoning:
                messages.append({"type": "thinking", "content": reasoning})

            if text:
                messages.append({"type": "ai", "content": text})

            for tc in tool_calls:
                messages.append({"type": "tool_call", "name": tc["name"], "args": tc["args"]})

        elif isinstance(msg, ToolMessage):
            if msg.name and msg.name.startswith(_HANDOFF_PREFIXES):
                continue  # internal supervisor handoff result (historical threads)
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
