# LangChain/LangGraph Documentation Summary

Reference summaries from reading the full docs. Each section notes how it applies to rhiza-agents.

## create_agent (langchain.agents)

`create_agent` is the primary agent constructor. It builds a LangGraph graph internally.

**Key params:** model, tools, system_prompt, middleware, state_schema, name, checkpointer, context_schema, store

**How we use it:** Replaces `create_react_agent` for all worker agents. Each worker gets its own middleware stack.

**Important details:**
- `model` accepts string shorthand ("anthropic:claude-sonnet-4-20250514") or ChatAnthropic instance
- `system_prompt` accepts str or SystemMessage (with cache_control for Anthropic prompt caching)
- `state_schema` must be TypedDict (not Pydantic) as of langchain 1.0
- `name` should be snake_case — providers reject spaces/special chars
- Empty tools list creates a single-LLM-node agent (no tool calling)
- Supports structured output via `response_format` param

**Dynamic tools:** Middleware can filter tools per-request based on state, store, or runtime context. Uses `@wrap_model_call` to override `request.tools`. For runtime-registered tools, also need `wrap_tool_call` hook so agent knows how to execute them.

**Dynamic prompts:** `@dynamic_prompt` decorator modifies system prompt per-request based on runtime context.

**Applies to our system:**
- Worker agents use create_agent with middleware
- Dynamic tool filtering could gate sandbox tools based on execution mode (review vs auto)
- The context_schema param could carry user_id and execution_mode from the request
- Prompt caching via SystemMessage cache_control would reduce costs for repeated conversations

## Models

**Key details:**
- `init_chat_model` for provider-agnostic initialization
- `max_retries` param (default 6) built into model — we were setting 3, could just use default
- Streaming: `astream_events()` for semantic event filtering
- Reasoning/thinking: provider-specific params control extended thinking
- Rate limiting: `InMemoryRateLimiter` at model init
- Token usage available on AIMessage objects

**Applies to our system:**
- We can use string shorthand "anthropic:claude-sonnet-4-20250514" instead of ChatAnthropic()
- ModelRetryMiddleware is better than model-level max_retries for consistent retry across the stack
- Reasoning tokens are the answer for Phase 10 (extended thinking)
- Token usage tracking could feed into cost monitoring

## Middleware Architecture

Four categories: monitoring, transformation, reliability, safety.

Hooks: before_model, after_model, wrap_model_call, wrap_tool_call, before_agent, after_agent.

Middleware is ordered — applied in list order for before hooks, reverse order for after hooks.

**Applies to our system:**
- We use SummarizationMiddleware (reliability/transformation)
- ModelRetryMiddleware (reliability)
- ModelCallLimitMiddleware (reliability)
- HumanInTheLoopMiddleware (safety)
- Could add PIIMiddleware for sensitive data
- Could add ContextEditingMiddleware as a complement to summarization

## Custom Middleware

Two approaches: decorator-based (simple, single hook) and class-based (complex, multi-hook).

**Hooks available:**
- Node-style: `before_agent`, `before_model`, `after_model`, `after_agent` — sequential, good for logging/validation/state updates
- Wrap-style: `wrap_model_call`, `wrap_tool_call` — nested like function calls, good for retry/caching/transformation

**State updates from middleware:**
- Node-style: return a dict to merge into state
- Wrap-style: return `ExtendedModelResponse` with a `Command` to update state
- Multiple middleware commands compose through reducers (inner-first, then outer)

**Agent jumps:** Middleware can exit early via `jump_to` with targets "end", "tools", or "model". Requires `@hook_config(can_jump_to=[...])` decorator.

**Execution order:** Before hooks run first-to-last, wrap hooks nest (outer wraps inner), after hooks run last-to-first.

**Applies to our system:**
- Custom middleware for execution mode (review vs auto) could use `wrap_model_call` to filter tools based on state
- `before_agent` hook could inject per-user context
- `after_model` with `jump_to="end"` could enforce output constraints

## Built-in Middleware (Complete Inventory)

16 provider-agnostic + provider-specific:

1. **SummarizationMiddleware** — auto-compress conversation history at token thresholds. Config: model, trigger (tokens/messages/fraction), keep (messages count).
2. **HumanInTheLoopMiddleware** — pause for human approval. Config: interrupt_on (tool→approval config map), description_prefix. Requires checkpointer.
3. **ModelCallLimitMiddleware** — cap LLM API calls. Config: thread_limit, run_limit, exit_behavior (end/error).
4. **ToolCallLimitMiddleware** — restrict per-tool or global calls. Config: tool_name, thread_limit, run_limit, exit_behavior (continue/error/end).
5. **ModelRetryMiddleware** — retry failed model calls with backoff. Config: max_retries, retry_on, on_failure, backoff params.
6. **ToolRetryMiddleware** — retry failed tool calls with backoff. Config: max_retries, tools, retry_on, on_failure, backoff params.
7. **ModelFallbackMiddleware** — chain fallback models. Config: first_model, *additional_models.
8. **PIIMiddleware** — detect/handle PII. Config: pii_type, strategy (block/redact/mask/hash), detector, scope flags.
9. **TodoListMiddleware** — task planning tools. Config: system_prompt, tool_description.
10. **LLMToolSelectorMiddleware** — LLM filters relevant tools. Config: model, system_prompt, max_tools, always_include.
11. **ContextEditingMiddleware** — clear old tool outputs. Config: edits (ClearToolUsesEdit), token_count_method, trigger, keep, exclude_tools.
12. **ShellToolMiddleware** — persistent shell sessions. Config: workspace_root, execution_policy (Host/Docker/Codex), env, redaction_rules.
13. **FilesystemFileSearchMiddleware** — glob/grep over files. Config: root_path, use_ripgrep, max_file_size_mb.
14. **LLMToolEmulator** — simulate tools for testing. Config: tools, model.
15. **SubagentMiddleware** — spawn isolated task agents. Config: default_model, default_tools, subagents array.
16. **FilesystemMiddleware** (Deep Agents) — ls, read_file, write_file, edit_file with backend routing.

**Provider-specific:** Anthropic (prompt caching, bash tool, text editor, memory, file search), AWS (prompt caching), OpenAI (content moderation).

## Human-in-the-Loop

HITL middleware pauses execution via `interrupt()` when tool calls need review.

**Config:**
```python
HumanInTheLoopMiddleware(
    interrupt_on={
        "dangerous_tool": True,                    # all decisions
        "semi_dangerous": {"allowed_decisions": ["approve", "reject"]},
        "safe_tool": False,                        # auto-approve
    }
)
```

**Decisions:** approve, edit (modify args), reject (with message added to conversation).

**Streaming with interrupts:** Use `stream_mode=["updates", "messages"]`, check for `__interrupt__` in updates chunks. Resume with `Command(resume={"decisions": [...]})`.

**Applies to our system:**
- `execute_python_code` and `run_file` get HITL when review mode is active
- The interrupt value contains action_requests describing what needs approval
- UI shows pending tool call, user clicks approve/reject
- Resume sends Command with decisions back to the same thread_id

## Streaming

**Stream modes:**
- `updates` — state changes after each step (model output, tool results)
- `messages` — token-by-token from LLM with metadata
- `custom` — arbitrary signals via `get_stream_writer()` from tools

**Reasoning tokens in streams:** Filter `stream_mode="messages"` chunks for `content_blocks` with `type: "reasoning"`. LangChain normalizes Anthropic thinking blocks and OpenAI reasoning summaries into standard `"reasoning"` type.

**Sub-agent identification:** `lc_agent_name` in metadata identifies which agent is emitting tokens. Use `subgraphs=True` for sub-agent output.

**V2 format:** `version="v2"` gives consistent `StreamPart` dicts with `type`, `ns`, `data` keys. `invoke()` returns `GraphOutput` with `.value` and `.interrupts`.

**Applies to our system:**
- Switch to `stream_mode=["updates", "messages"]` with `version="v2"` for cleaner event handling
- Use `content_blocks` with type "reasoning" for Phase 10 instead of tag parsing
- `lc_agent_name` metadata replaces our custom agent name tracking
- `__interrupt__` in updates handles HITL pauses
- `get_stream_writer()` in tools can emit file_changed events directly

## Multi-Agent: Subagents

Main agent invokes subagents as tools. Subagents are stateless — memory stays with main agent.

**Patterns:** tool-per-agent (wrap each as @tool) or single dispatch (one routing tool).

**Context:** `ToolRuntime` provides access to parent state. Return `Command` with state updates from subagent tools.

**Persistence:** `checkpointer=True` enables continuations (persistent subagent state across calls).

**Applies to our system:**
- Our worker agents (data_analyst, code_runner) could be subagent tools on a single main agent
- This would simplify the architecture vs the current supervisor/handoff pattern
- But we currently use create_supervisor which handles handoffs automatically

## Multi-Agent: Handoffs

**Key recommendation from docs: "Use single agent with middleware for most handoff use cases — it's simpler."**

Two approaches:
1. **Single agent + middleware** — One agent changes behavior based on state. `@wrap_model_call` dynamically adjusts prompts and tools based on `current_step` state.
2. **Multiple agent subgraphs** — Separate agents as graph nodes with `Command(goto=..., graph=Command.PARENT)` for handoffs. More complex, only for bespoke agent implementations.

**Applies to our system:**
- We use approach 2 (create_supervisor + worker subgraphs) — this is our architecture
- Approach 1 exists if needed: single agent + wrap_model_call to swap prompts/tools per state
- Both approaches support middleware, HITL, and streaming

## Messages

**Types:** SystemMessage, HumanMessage, AIMessage, ToolMessage

**Content blocks (standardized):**
- `TextContentBlock` — regular text
- `ReasoningContentBlock` — model thinking/reasoning (key for Phase 10)
- `ImageContentBlock`, `AudioContentBlock`, `VideoContentBlock`, `FileContentBlock` — multimodal
- `ToolCall`, `ToolCallChunk`, `InvalidToolCall` — tool calling
- `ServerToolCall`, `ServerToolResult` — server-side tool execution

**AIMessage key attributes:** text, content, content_blocks, tool_calls, id, usage_metadata, response_metadata

**Applies to our system:**
- `content_blocks` with `ReasoningContentBlock` is the protocol-level thinking solution
- No more parsing `[THINKING]`/`[RESPONSE]` tags
- `usage_metadata` on AIMessage could feed cost tracking

## Reasoning Tokens (Frontend)

**Content block types:** `{ type: "reasoning", reasoning: "..." }` vs `{ type: "text", text: "..." }`

**UI pattern:** Collapsible ThinkingBubble component. Collapsed by default during streaming (show spinner only). Distinct styling (light purple background). Only expand after reasoning completes to avoid layout jitter.

**Edge cases:** Not all messages have reasoning blocks. Multiple reasoning blocks per message possible — concatenate. Some messages alternate reasoning/text cycles.

**Applies to our system:**
- Phase 10 should use this pattern exactly
- Activity panel becomes the thinking display
- During streaming, show spinner in activity panel while reasoning, then show text in chat
- On page reload, reasoning blocks render in activity panel, text blocks in chat

## Tools

**@tool decorator:** Type hints define input schema, docstrings become descriptions. `snake_case` names for provider compatibility.

**Reserved params:** `config` and `runtime` are reserved — cannot be tool arguments. `runtime: ToolRuntime` is auto-injected and hidden from LLM.

**ToolRuntime provides:**
- `runtime.state` — current graph state (messages, custom fields)
- `runtime.context` — immutable per-invocation context (user_id, etc)
- `runtime.store` — persistent storage across conversations (namespace/key pattern)
- `runtime.stream_writer` — emit real-time updates during tool execution
- `runtime.tool_call_id` — correlate invocations
- `runtime.config` — RunnableConfig for callbacks/tags/metadata

**Command returns:** Tools return `Command(update={...})` to update graph state. Must include `ToolMessage` with matching `tool_call_id` in the update.

**Stream writer:** `runtime.stream_writer(msg)` emits custom events during tool execution. Tool must run in LangGraph context. Use with `stream_mode="custom"`.

**Applies to our system:**
- `write_file` and `run_file` correctly use `runtime` and return `Command`
- `runtime.stream_writer` could replace our `files_changed` SSE event hack — emit file change events directly from the tool
- `runtime.context` could carry `user_id` and `execution_mode` from the request
- `runtime.store` could be used for persistent per-user settings

## RAG

Two patterns:
1. **RAG Agent** — retrieval tool called by the agent when needed. Two LLM calls but more flexible.
2. **RAG Chain** — `@dynamic_prompt` middleware injects context before every model call. One LLM call but always searches.

Advanced: `RetrieveDocumentsMiddleware` using `before_model` hook to augment messages with retrieved context and store source docs in custom state.

**Applies to our system:**
- Our vector store retrieval tools follow the RAG Agent pattern (tool-based)
- Could use middleware pattern for always-on retrieval in research_assistant
- `@tool(response_format="content_and_artifact")` returns both content for LLM and raw docs for programmatic access

## Quickstart Patterns

Key patterns from quickstart:
- `context_schema` with `@dataclass` for passing user context at invocation time
- `response_format=ToolStrategy(Schema)` for structured output
- `checkpointer=InMemorySaver()` for conversation persistence
- Invoke with `context=Context(user_id="1")` to pass per-request context

**Applies to our system:**
- We should pass `context={"user_id": user_id, "execution_mode": mode}` at invocation time
- The context_schema approach is cleaner than passing config through RunnableConfig

## Structured Output

`response_format` param on `create_agent`:
- `ToolStrategy(Schema)` — uses tool calling (works with any model)
- `ProviderStrategy(Schema)` — uses native provider structured output (Anthropic, OpenAI)
- Pass schema type directly — auto-selects best strategy

Structured response in `result['structured_response']`. Supports Pydantic, dataclass, TypedDict, JSON Schema, Union types.

Error handling: `handle_errors=True` (default) retries with feedback on validation failures.

## Deep Agents

Standalone library built on LangChain. Provides:
- Planning (write_todos tool)
- File system tools (ls, read_file, write_file, edit_file) with pluggable backends
- Subagent spawning (task tool)
- Long-term memory via LangGraph Store

Backends: StateBackend (in-memory/checkpoint), FilesystemBackend (disk), StoreBackend (LangGraph store), SandboxBackend (Modal/Daytona/Deno), CompositeBackend (route by path prefix).

The deep-agents-ui reads files from graph state and displays them in a file viewer — this is the pattern our Phase 9 file viewer follows.

## Pages Read

- /oss/python/langchain/agents (create_agent, tools, prompts, middleware, streaming, structured output)
- /oss/python/langchain/models (init_chat_model, providers, streaming, reasoning, rate limiting)
- /oss/python/langchain/middleware (architecture overview)
- /oss/python/langchain/middleware/built-in (all 16 middleware)
- /oss/python/langchain/middleware/custom (hooks, decorators, classes, state management, execution order)
- /oss/python/langchain/human-in-the-loop (HITL middleware, interrupt/resume, approve/edit/reject)
- /oss/python/langchain/streaming (stream modes, reasoning tokens, interrupts, sub-agents, v2 format)
- /oss/python/langchain/multi-agent/subagents (tool-per-agent, dispatch, context, persistence)
- /oss/python/langchain/multi-agent/handoffs (single agent + middleware vs subgraphs, Command routing)
- /oss/python/langchain/messages (types, content blocks, reasoning blocks, standard blocks)
- /oss/python/langchain/tools (@tool, ToolRuntime, Command returns, stream_writer)
- /oss/python/langchain/rag (agent pattern, chain pattern, middleware pattern)
- /oss/python/langchain/structured-output (ToolStrategy, ProviderStrategy, error handling)
- /oss/python/langchain/frontend/reasoning-tokens (content blocks, UI patterns, streaming)
- /oss/python/langchain/frontend/structured-output (rendering, streaming partial data)
- /oss/python/langchain/quickstart (full agent example with context, structured output, checkpointer)
- /oss/python/langchain/install (package structure)
- /oss/python/deepagents/overview (architecture, backends, built-in tools)
- /oss/python/langgraph/overview (low-level orchestration, durable execution)
