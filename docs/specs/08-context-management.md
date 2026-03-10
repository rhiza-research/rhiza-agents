# Phase 8: Context Management

## Goal

Long conversations don't break the system. As messages accumulate, agents stay within their context window by trimming older messages before each LLM call. When the checkpoint history grows too large, old messages are summarized and pruned so that the checkpointer doesn't grow unbounded and conversation reloads stay fast.

## Prerequisites

Phase 7 must be complete and working:
- Full multi-agent system deployed to production
- All features functional (MCP tools, sandbox, vector stores, streaming)

## Problem

With `output_mode="full_history"`, the supervisor sees all sub-agent messages, tool calls, and tool results. A single multi-step agent turn can add 10+ messages. After a few exchanges, the full history can exceed the model's context window, causing failures. Even before that point, performance degrades and costs increase as the context grows.

Two problems to solve:
1. **Per-LLM-call trimming** -- keep each model invocation within token limits regardless of checkpoint size
2. **Checkpoint pruning** -- prevent the checkpointer from growing unbounded over long conversations

## Files to Modify

```
src/rhiza_agents/agents/graph.py
src/rhiza_agents/main.py
```

## Key APIs & Packages

```python
# Message trimming (already available via langchain-core)
from langchain_core.messages.utils import trim_messages, count_tokens_approximately

# Message removal from checkpoint
from langchain_core.messages import RemoveMessage, SystemMessage
```

No new packages needed. `trim_messages` and `count_tokens_approximately` are in `langchain-core`, which is already a dependency.

## Implementation Details

### Part 1: Per-LLM-Call Message Trimming (`graph.py`)

Each agent (supervisor and workers) should trim messages before they reach the LLM. This doesn't change what's stored in the checkpoint -- it only controls what the model sees.

**Worker agents** -- `create_react_agent` accepts a `prompt` parameter that can be a callable. Use it to prepend the system prompt and trim messages:

```python
from langchain_core.messages.utils import trim_messages, count_tokens_approximately
from langchain_core.messages import SystemMessage

def _make_prompt_with_trimming(system_prompt: str, max_tokens: int = 100_000):
    """Create a prompt callable that trims messages to stay within token limits.

    Args:
        system_prompt: The agent's system prompt text.
        max_tokens: Maximum tokens for the message history (excluding system prompt).
                    Default 100k leaves headroom within Claude's 200k context window
                    for the system prompt, tool definitions, and response.
    """
    def prompt(messages: list) -> list:
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
```

Then in `build_graph`, when creating worker agents:

```python
agent = create_react_agent(
    model=model,
    tools=tools,
    prompt=_make_prompt_with_trimming(config.system_prompt),
    name=config.id,
)
```

**Supervisor agent** -- `create_supervisor` also accepts a `prompt` parameter. Use the same pattern:

```python
supervisor = create_supervisor(
    model=ChatAnthropic(model=supervisor_config.model).with_retry(stop_after_attempt=3),
    agents=worker_agents,
    prompt=_make_prompt_with_trimming(supervisor_config.system_prompt),
    output_mode="full_history",
    add_handoff_back_messages=True,
)
```

Check whether `create_supervisor`'s `prompt` parameter accepts a callable the same way `create_react_agent` does. If it only accepts a string, pass the system prompt string directly and instead apply trimming at the model level using a wrapper or by trimming in `main.py` before invocation.

### Part 2: Checkpoint Summarization (`main.py`)

After a graph invocation completes, check if the conversation's message history has grown too large. If so, summarize old messages and prune the checkpoint.

**Threshold and strategy:**

```python
MESSAGE_COUNT_THRESHOLD = 50  # Trigger summarization when messages exceed this count
MESSAGES_TO_KEEP = 10         # Keep the N most recent messages after summarization
```

**Summarization function:**

```python
async def _maybe_summarize(graph, conversation_id: str, model):
    """Summarize and prune checkpoint if message history is too long."""
    state = await graph.aget_state({"configurable": {"thread_id": conversation_id}})
    messages = state.values.get("messages", [])

    if len(messages) <= MESSAGE_COUNT_THRESHOLD:
        return

    # Split into messages to summarize and messages to keep
    to_summarize = messages[:-MESSAGES_TO_KEEP]
    existing_summary = ""

    # Check if there's already a summary message at the start
    if to_summarize and isinstance(to_summarize[0], SystemMessage) and to_summarize[0].content.startswith("Summary of earlier conversation:"):
        existing_summary = to_summarize[0].content
        to_summarize = to_summarize[1:]

    # Build summarization prompt
    if existing_summary:
        prompt = (
            f"{existing_summary}\n\n"
            "Update this summary to include the following new messages. "
            "Be concise but preserve key facts, decisions, and results:\n\n"
        )
    else:
        prompt = (
            "Summarize the following conversation. Be concise but preserve "
            "key facts, decisions, data results, and any code that was executed. "
            "This summary will replace the original messages:\n\n"
        )

    # Format messages for summarization
    for msg in to_summarize:
        role = msg.__class__.__name__.replace("Message", "")
        content = _extract_text(msg.content) if hasattr(msg, "content") else ""
        if content:
            prompt += f"{role}: {content[:500]}\n"

    summary_response = await model.ainvoke([HumanMessage(content=prompt)])

    # Update the checkpoint: remove old messages, prepend summary
    updates = []
    for msg in to_summarize:
        updates.append(RemoveMessage(id=msg.id))

    await graph.aupdate_state(
        {"configurable": {"thread_id": conversation_id}},
        {"messages": updates},
    )
```

**Integration into POST /api/chat and POST /api/chat/stream:**

After the graph invocation completes (and after sending the response/stream), call `_maybe_summarize` as a background task so it doesn't block the response:

```python
# After the response is sent
asyncio.create_task(_maybe_summarize(graph, conversation_id, summary_model))
```

Use a smaller/cheaper model for summarization if available (e.g., Haiku), or the same model.

### Interaction with `_process_messages()`

The existing `_process_messages()` function processes whatever is in the checkpoint. After summarization, the checkpoint contains fewer messages. This is transparent -- `_process_messages()` just processes what's there. The summary message (if stored as a SystemMessage) would be filtered out by the existing logic since `_process_messages()` only outputs `human`, `ai`, `thinking`, `tool_call`, and `tool_result` types.

If the summary should be visible to the user when they reload a long conversation, consider prepending it as a special `ai` type message with a distinct marker (e.g., `[SUMMARY]` prefix) so the UI can render it differently. This is optional -- without it, users just see the most recent messages after a reload, which may be acceptable.

### Interaction with Streaming (Phase 6)

The `recursion_limit` added in Phase 4 already applies to streaming via `.astream_events()`. The message trimming in Part 1 also applies -- it affects what the LLM sees regardless of whether the invocation is streaming or synchronous.

The post-invocation summarization (Part 2) runs the same way for both endpoints -- as a background task after the response/stream completes.

## Reference Files

| File | What to learn |
|------|---------------|
| `/Users/tristan/Devel/rhiza/rhiza-agents/src/rhiza_agents/agents/graph.py` | Agent and supervisor creation to modify |
| `/Users/tristan/Devel/rhiza/rhiza-agents/src/rhiza_agents/main.py` | Chat endpoints to add summarization |
| `/Users/tristan/Devel/rhiza/rhiza-agents/docs/ARCHITECTURE.md` | Message processing flow |

For LangGraph summarization patterns:
- `trim_messages(messages, strategy="last", token_counter=..., max_tokens=...)` -- trims message list to fit token budget
- `count_tokens_approximately(messages)` -- estimates token count without calling the model's tokenizer
- `RemoveMessage(id=msg.id)` -- used with `graph.aupdate_state()` to remove messages from checkpoint
- `graph.aupdate_state(config, values)` -- updates checkpoint state directly

## Acceptance Criteria

1. Start a conversation and send 20+ back-and-forth messages with tool calls
2. The agent continues to respond correctly without context window errors
3. Reload the conversation page -- recent messages display correctly
4. After summarization triggers, older messages are replaced by a summary in the checkpoint
5. New messages after summarization still work correctly (the agent has context from the summary)
6. The activity panel shows recent tool calls correctly (older ones are pruned)
7. Summarization runs as a background task and doesn't delay the chat response

## What NOT to Do

- **No user-facing controls for summarization** -- it happens automatically in the background. No settings to configure threshold or strategy.
- **No per-agent summarization strategies** -- all agents use the same trimming config. Different agents don't need different context window sizes.
- **No external memory store** -- summaries live in the checkpoint state, not in a separate database.
- **No semantic memory or entity extraction** -- just straightforward message summarization. Don't build a knowledge graph or entity store.
- **No changes to the database schema** -- everything works through the existing LangGraph checkpoint mechanism.
