# Phase 10: Extended Thinking

## Goal

Replace the convention-based `[THINKING]`/`[RESPONSE]` tag system with Anthropic's native extended thinking API. Thinking and response content become structurally distinct at the protocol level, eliminating reliance on the LLM following prompt instructions.

## Problem

The current system uses prompt instructions to get agents to tag their output with `[THINKING]` and `[RESPONSE]`. This is unreliable:

1. **The LLM doesn't always comply.** Thinking text leaks into the main chat body when the model skips or misplaces tags.
2. **Streaming classification is fragile.** The server buffers tokens to detect the tag prefix, which is hacky and adds latency.
3. **It's convention, not protocol.** Security and UX correctness should not depend on LLM behavior.

## Solution

Enable extended thinking on `ChatAnthropic` model instances. The Anthropic API produces `thinking` content blocks (for reasoning) and `text` content blocks (for the response) as structurally distinct types. The streaming handler routes them by block type, not by parsing text content.

## Prerequisites

Phase 9 complete, including the streaming modernization (switch from `astream_events` to `graph.astream` with `stream_mode=["updates", "messages", "custom"]`). The `"messages"` stream mode provides `content_blocks` with `type: "reasoning"` vs `type: "text"`, which is required for this phase. `langchain-anthropic` supports the `thinking` parameter (verified: `ChatAnthropic(model=..., thinking={'type': 'enabled', 'budget_tokens': N})`).

## What to Change

### Model Configuration

Enable extended thinking on all `ChatAnthropic` instances in `graph.py`:

```python
ChatAnthropic(model=wc.model, max_retries=3, thinking={'type': 'enabled', 'budget_tokens': 10000})
```

The `budget_tokens` controls how many tokens the model can use for thinking. Needs experimentation to find the right balance — too low and the model can't reason properly, too high and it wastes time/tokens on simple questions.

### Streaming Handler

In the `"messages"` stream (from Phase 9's streaming modernization), filter `chunk.content_blocks` for `type: "reasoning"` (→ activity panel `thinking` SSE event) vs `type: "text"` (→ `token` SSE event). LangChain normalizes Anthropic thinking blocks into the standard `"reasoning"` content block type.

The current tag buffering code (`node_buffer`, `node_mode`, `_THINKING_TAG`, `_RESPONSE_TAG` detection) should be removed entirely.

### Agent Prompts

Remove `[THINKING]`/`[RESPONSE]` tag instructions from all agent system prompts in `registry.py`. The model no longer needs to self-tag its output — the API handles the separation structurally.

### Message Processing

Update `_process_messages()` and `_classify_text()` in `main.py` for page reloads. These currently look for `[THINKING]`/`[RESPONSE]` tags in stored messages. With extended thinking, the stored messages will have distinct content block types instead.

## What to Validate

Before implementing broadly, test with a single agent:

1. Enable thinking on the code_runner model only
2. Remove the `[THINKING]`/`[RESPONSE]` tags from its prompt
3. Send a code execution request
4. Verify:
   - Reasoning appears in the activity panel (thinking blocks)
   - Only the final response appears in the chat (text blocks)
   - No reasoning leaks into the chat body
   - Streaming works correctly
   - Page reload correctly separates thinking from response

If the model's text output still contains reasoning that should be hidden, extended thinking alone may not be sufficient and additional measures would be needed.

## What NOT to Do

- No prompt-based thinking classification — the whole point is to remove it
- No fallback to tag parsing — if extended thinking doesn't work cleanly, address it rather than re-adding tags
- No different thinking budgets per agent — keep it uniform initially
- No exposing thinking budget as a user-configurable setting
- No changes to the database schema

## Acceptance Criteria

1. Agents produce clean responses in the chat body with no reasoning mixed in
2. Reasoning appears in the activity panel via the API's thinking blocks
3. Streaming correctly routes thinking to activity and text to chat in real-time
4. Page reloads correctly classify stored messages
5. No `[THINKING]` or `[RESPONSE]` tags in any agent prompts
6. Works with all three agents (supervisor, data_analyst, code_runner)

---

## Implementation Notes

### What was built

**Model configuration** (`agents/graph.py`):
- All `ChatAnthropic` instances (worker and supervisor) now use `thinking={"type": "enabled", "budget_tokens": 10000}` with `max_tokens=16000`
- `budget_tokens` must be less than `max_tokens` per Anthropic API requirements

**Streaming handler** (`main.py`):
- New `_extract_content_blocks(content)` function returns `(text, reasoning)` by inspecting content block types
- Handles both dict-style blocks (`{"type": "reasoning", "reasoning": "..."}`) and object-style blocks (`block.type == "reasoning"`)
- Streaming handler directly routes: reasoning blocks → `thinking` SSE event, text blocks → `token` SSE event
- Removed all tag buffering code: `node_buffer`, `node_mode`, `_THINKING_TAG`, `_RESPONSE_TAG`, `_classify_text()`

**Agent prompts** (`agents/registry.py`):
- Removed `_OUTPUT_FORMAT` constant with `[THINKING]`/`[RESPONSE]` tag instructions
- Simplified all worker prompts to plain instructions without output format tags
- Supervisor prompt unchanged (it doesn't produce thinking text)

**Message processing** (`main.py` `_process_messages`):
- Uses `_extract_content_blocks()` to separate reasoning and text from stored AIMessages
- Reasoning content emitted as `{"type": "thinking"}` entries, text as `{"type": "ai"}` entries
- No fallback to tag parsing — clean break from the old system

### Constraints

- Extended thinking requires `tool_choice: "auto"` (default) — cannot force specific tools
- `budget_tokens` (10000) is uniform across all agents as specified
- The model may not use the full thinking budget on simple queries
