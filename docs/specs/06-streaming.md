# Phase 6: Streaming + Polish

## Goal

Responses stream to the UI in real-time using Server-Sent Events (SSE). Users see tokens appear as they are generated, tool calls appear as they happen, and agent handoffs are visible. This dramatically improves the user experience for long-running agent tasks.

## Prerequisites

Phase 5 must be complete and working:
- Full multi-agent system with supervisor, MCP tools, sandbox, and vector stores
- All features functional via the synchronous POST /api/chat endpoint

## Files to Modify

```
src/rhiza_agents/main.py
src/rhiza_agents/templates/chat.html
src/rhiza_agents/static/chat.js
src/rhiza_agents/static/style.css
```

## Key APIs & Packages

```python
# SSE streaming response
from fastapi.responses import StreamingResponse
from starlette.responses import Response

# LangGraph streaming
# The compiled graph has .astream_events() method
# graph.astream_events(input, config, version="v2")

# JSON for SSE data
import json
```

No new packages needed. `fastapi` and `langgraph` already support streaming.

## Implementation Details

### Modifications to `main.py` -- Streaming Endpoint

**New route:**

```
POST /api/chat/stream
```

This is a POST (not GET) because it sends a message body. It returns an SSE stream.

Request body (same as POST /api/chat):
```json
{
    "message": "string",
    "conversation_id": "string | null"
}
```

**Handler implementation:**

```python
@app.post("/api/chat/stream")
async def stream_chat_message(
    request: Request,
    body: SendMessageRequest,
    user: dict = Depends(require_auth),
):
    user_id = get_user_id(request)

    # Get or create conversation (same logic as POST /api/chat)
    if body.conversation_id:
        conversation = await db.get_conversation(body.conversation_id, user_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        conversation_id = body.conversation_id
    else:
        conversation_id = str(uuid.uuid4())
        await db.create_conversation(conversation_id, user_id)

    # Build the graph
    graph = await get_agent_graph(user_id, db, mcp_tools, checkpointer,
                                   daytona_api_key=config.daytona_api_key,
                                   vectorstore_manager=vectorstore_manager)

    async def event_generator():
        # Send conversation_id as the first event (needed for new conversations)
        yield f"event: conversation_id\ndata: {json.dumps({'conversation_id': conversation_id})}\n\n"

        current_agent = None
        tool_calls = []

        try:
            async for event in graph.astream_events(
                {"messages": [HumanMessage(content=body.message)]},
                config={"configurable": {"thread_id": conversation_id}},
                version="v2",
            ):
                kind = event["event"]

                # Token streaming from LLM
                if kind == "on_chat_model_stream":
                    chunk = event["data"]["chunk"]
                    # Check if this is a text content chunk
                    if hasattr(chunk, "content") and isinstance(chunk.content, str) and chunk.content:
                        # Determine which agent is generating
                        agent_name = event.get("metadata", {}).get("langgraph_node", "")
                        if agent_name != current_agent:
                            current_agent = agent_name
                            yield f"event: agent_start\ndata: {json.dumps({'agent': agent_name})}\n\n"
                        yield f"event: token\ndata: {json.dumps({'content': chunk.content})}\n\n"

                # Tool call started
                elif kind == "on_tool_start":
                    tool_name = event.get("name", "")
                    tool_input = event.get("data", {}).get("input", {})
                    yield f"event: tool_start\ndata: {json.dumps({'name': tool_name, 'input': tool_input})}\n\n"
                    tool_calls.append({"name": tool_name, "input": tool_input})

                # Tool call completed
                elif kind == "on_tool_end":
                    tool_name = event.get("name", "")
                    tool_output = event.get("data", {}).get("output", "")
                    # Convert tool output to string if it's not already
                    if hasattr(tool_output, "content"):
                        tool_output = tool_output.content
                    yield f"event: tool_end\ndata: {json.dumps({'name': tool_name, 'output': str(tool_output)[:1000]})}\n\n"

        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

        # Update conversation metadata
        await db.touch_conversation(conversation_id)
        conversation = await db.get_conversation(conversation_id, user_id)
        if conversation and not conversation.get("title"):
            title = body.message[:50] + ("..." if len(body.message) > 50 else "")
            await db.update_conversation_title(conversation_id, user_id, title)

        # Send completion event
        yield f"event: done\ndata: {json.dumps({'tool_calls': tool_calls})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )
```

**Keep the existing POST /api/chat endpoint** -- it still works for non-streaming clients. The frontend switches to using the streaming endpoint.

### SSE Event Types

| Event | Data | Purpose |
|-------|------|---------|
| `conversation_id` | `{"conversation_id": "..."}` | Sent first, provides ID for new conversations |
| `agent_start` | `{"agent": "data_analyst"}` | Agent handoff -- new agent is processing |
| `token` | `{"content": "..."}` | Text token from LLM response |
| `tool_start` | `{"name": "...", "input": {...}}` | Tool call initiated |
| `tool_end` | `{"name": "...", "output": "..."}` | Tool call completed (output truncated to 1000 chars) |
| `error` | `{"error": "..."}` | Error occurred during processing |
| `done` | `{"tool_calls": [...]}` | Stream complete, includes summary of all tool calls |

### Modifications to `templates/chat.html`

Minimal changes -- the template still renders server-side messages the same way. The main change is that the "Thinking..." loading state is replaced by actual streaming content.

No structural HTML changes needed. The JavaScript handles all the streaming UI.

### Modifications to `static/chat.js`

Replace the `fetch` call in the form submit handler with an SSE stream consumer.

**Key changes:**

1. Replace the form submit handler's fetch with streaming:

```javascript
// Instead of:
// const response = await fetch('/api/chat', {...});

// Use:
const response = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
        message: message,
        conversation_id: conversationIdInput.value || null
    })
});

if (!response.ok) {
    throw new Error(`Server error ${response.status}`);
}

const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = '';

// Create the assistant message div (empty, will be filled by stream)
const assistantMsg = addStreamingMessage();

while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // Parse SSE events from buffer
    const events = parseSSEEvents(buffer);
    buffer = events.remaining;

    for (const event of events.parsed) {
        handleStreamEvent(event, assistantMsg);
    }
}
```

2. **SSE event parser:**

```javascript
function parseSSEEvents(buffer) {
    const parsed = [];
    const lines = buffer.split('\n');
    let remaining = '';
    let currentEvent = null;

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];

        if (line.startsWith('event: ')) {
            currentEvent = { type: line.slice(7).trim(), data: '' };
        } else if (line.startsWith('data: ') && currentEvent) {
            currentEvent.data = line.slice(6);
        } else if (line === '' && currentEvent) {
            try {
                currentEvent.data = JSON.parse(currentEvent.data);
            } catch (e) {}
            parsed.push(currentEvent);
            currentEvent = null;
        }
    }

    // Keep unparsed data in buffer
    if (currentEvent) {
        remaining = `event: ${currentEvent.type}\ndata: ${currentEvent.data}\n`;
    }

    return { parsed, remaining };
}
```

3. **Stream event handler:**

```javascript
function handleStreamEvent(event, msgDiv) {
    switch (event.type) {
        case 'conversation_id':
            if (!conversationIdInput.value) {
                conversationIdInput.value = event.data.conversation_id;
                history.pushState({}, '', `/c/${event.data.conversation_id}`);
            }
            break;

        case 'agent_start':
            updateAgentBadge(msgDiv, event.data.agent);
            break;

        case 'token':
            appendToken(msgDiv, event.data.content);
            break;

        case 'tool_start':
            addToolCallIndicator(msgDiv, event.data.name, event.data.input);
            break;

        case 'tool_end':
            updateToolCallResult(msgDiv, event.data.name, event.data.output);
            break;

        case 'error':
            setMessageError(msgDiv, event.data.error);
            break;

        case 'done':
            finalizeMessage(msgDiv, event.data.tool_calls);
            break;
    }
}
```

4. **Streaming message display:**

```javascript
let streamedContent = '';

function addStreamingMessage() {
    streamedContent = '';
    const div = document.createElement('div');
    div.className = 'message assistant streaming';
    div.innerHTML = `
        <div class="agent-badge-container"></div>
        <div class="message-content"></div>
        <div class="tool-calls-stream"></div>
    `;
    messagesDiv.appendChild(div);
    messagesDiv.scrollTop = messagesDiv.scrollHeight;
    return div;
}

function appendToken(msgDiv, token) {
    streamedContent += token;
    const contentDiv = msgDiv.querySelector('.message-content');
    contentDiv.innerHTML = renderMarkdown(streamedContent);
    messagesDiv.scrollTop = messagesDiv.scrollHeight;
}

function updateAgentBadge(msgDiv, agentName) {
    const container = msgDiv.querySelector('.agent-badge-container');
    // Format agent ID to display name
    const displayName = agentName.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    container.innerHTML = `<span class="agent-badge">${displayName}</span>`;
}

function addToolCallIndicator(msgDiv, toolName, toolInput) {
    const toolsDiv = msgDiv.querySelector('.tool-calls-stream');
    const toolDiv = document.createElement('div');
    toolDiv.className = 'tool-call-stream';
    toolDiv.dataset.toolName = toolName;
    toolDiv.innerHTML = `
        <span class="tool-name">${toolName}</span>
        <span class="tool-status running">running...</span>
    `;
    toolsDiv.appendChild(toolDiv);
    messagesDiv.scrollTop = messagesDiv.scrollHeight;
}

function updateToolCallResult(msgDiv, toolName, output) {
    const toolsDiv = msgDiv.querySelector('.tool-calls-stream');
    const toolDiv = toolsDiv.querySelector(`[data-tool-name="${toolName}"]`);
    if (toolDiv) {
        const status = toolDiv.querySelector('.tool-status');
        status.textContent = 'done';
        status.className = 'tool-status done';
    }
}

function finalizeMessage(msgDiv, toolCalls) {
    msgDiv.classList.remove('streaming');
    streamedContent = '';
}
```

### Modifications to `static/style.css`

Add styles for streaming state:

```css
/* Streaming indicator */
.message.streaming .message-content::after {
    content: "▊";
    animation: blink 1s infinite;
    color: #4a90d9;
}

@keyframes blink {
    0%, 50% { opacity: 1; }
    51%, 100% { opacity: 0; }
}

/* Agent badge container */
.agent-badge-container {
    margin-bottom: 0.25rem;
}

/* Tool call streaming indicators */
.tool-calls-stream {
    margin-top: 0.5rem;
}

.tool-call-stream {
    font-size: 0.8rem;
    color: #888;
    padding: 0.25rem 0;
    display: flex;
    align-items: center;
    gap: 0.5rem;
}

.tool-status {
    font-size: 0.75rem;
    padding: 0.1rem 0.4rem;
    border-radius: 3px;
}

.tool-status.running {
    color: #f0c040;
    background: rgba(240, 192, 64, 0.15);
    animation: pulse 2s infinite;
}

.tool-status.done {
    color: #4a9a4a;
    background: rgba(74, 154, 74, 0.15);
}

@keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
}
```

### Considerations

**Markdown rendering during streaming:**

Re-rendering the full accumulated content through `marked.parse()` on every token can be expensive. Two approaches:

1. **Simple approach (recommended for this phase):** Re-render on every token. With modern browsers and `marked`, this is fast enough for typical response lengths. The `renderMarkdown` function already exists.

2. **Debounced approach (optional optimization):** Only re-render every 100ms, accumulate tokens in between. This reduces DOM thrashing for very fast token streams.

Start with approach 1 and optimize only if there are visible performance issues.

**Error handling during stream:**

If the SSE stream disconnects unexpectedly (network error, server restart):
- The `reader.read()` will reject
- Catch the error, show an error message in the UI
- The conversation state is still persisted in the checkpointer (whatever was processed before the error)
- User can refresh and see the partial conversation

**Concurrent requests:**

The streaming endpoint should handle only one active stream per conversation at a time. If a user sends a new message while a stream is in progress, the frontend should cancel the previous stream (abort the fetch) before starting a new one.

```javascript
let activeAbortController = null;

// In form submit handler:
if (activeAbortController) {
    activeAbortController.abort();
}
activeAbortController = new AbortController();

const response = await fetch('/api/chat/stream', {
    signal: activeAbortController.signal,
    // ...
});
```

**astream_events version:**

Use `version="v2"` for `astream_events`. This is the current stable streaming format in LangGraph. The event structure differs between v1 and v2 -- v2 provides better metadata including the node name.

Key v2 event fields:
- `event["event"]` -- event type string (e.g., "on_chat_model_stream")
- `event["data"]` -- event-specific data
- `event["metadata"]` -- includes `langgraph_node` for the current agent name
- `event["name"]` -- for tool events, the tool name

## Reference Files

| File | What to learn |
|------|---------------|
| `/Users/tristan/Devel/rhiza/rhiza-agents/docs/ARCHITECTURE.md` | Overall architecture context |
| `/Users/tristan/Devel/rhiza/rhiza-agents/src/rhiza_agents/main.py` | Existing chat endpoint to complement |
| `/Users/tristan/Devel/rhiza/rhiza-agents/src/rhiza_agents/static/chat.js` | Existing JS to modify |
| `/Users/tristan/Devel/rhiza/rhiza-agents/src/rhiza_agents/static/style.css` | Existing styles to extend |
| `/Users/tristan/Devel/rhiza/rhiza-agents/src/rhiza_agents/templates/chat.html` | Existing template |

## Acceptance Criteria

1. Send a message -- response streams token-by-token in the UI (not all at once)
2. A blinking cursor appears at the end of the streaming text
3. When the stream completes, the cursor disappears
4. Agent badge appears at the top of the message as soon as the agent starts processing (e.g., "Data Analyst")
5. When the supervisor routes to a different agent, the agent badge updates
6. Tool calls appear in real-time as they start (with "running..." status)
7. Tool calls update to "done" when they complete
8. Send "list available forecast models" -- see MCP tool calls stream, then response tokens stream
9. Start a new conversation -- conversation_id is received via the first SSE event and URL updates
10. If an error occurs mid-stream, an error message appears in the chat
11. Refreshing the page loads the full conversation from the checkpointer (server-side rendering works as before)
12. The non-streaming POST /api/chat endpoint still works (backward compatibility)

## What NOT to Do

- **No WebSocket** -- SSE (Server-Sent Events) is simpler and sufficient. The communication is server-to-client only during streaming.
- **No voice or audio** -- text only.
- **No streaming of tool execution** -- tool start/end events are sent, but the tool's internal execution doesn't stream. The tool runs to completion and then the result event is sent.
- **No client-side conversation state management** -- the server (LangGraph checkpointer) is the source of truth. The client is just a renderer.
- **No typing indicators for other users** -- this is a single-user application.
