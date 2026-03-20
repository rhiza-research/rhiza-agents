import { marked } from 'https://cdn.jsdelivr.net/npm/marked@17/+esm';
import hljs from 'https://cdn.jsdelivr.net/npm/highlight.js@11/+esm';
import { markedHighlight } from 'https://cdn.jsdelivr.net/npm/marked-highlight@2/+esm';

const form = document.getElementById('chat-form');
const input = document.getElementById('message-input');
const messagesDiv = document.getElementById('messages');
const sendBtn = document.getElementById('send-btn');
const newChatBtn = document.getElementById('new-chat');
const conversationIdInput = document.getElementById('conversation-id');
const activityPanel = document.getElementById('activity-panel');
const activityContent = document.getElementById('activity-content');
const activityToggle = document.getElementById('activity-toggle');
const activityClose = document.getElementById('activity-close');

// Configure marked.js with highlight.js
marked.use(markedHighlight({
    langPrefix: 'hljs language-',
    highlight: function(code, lang) {
        if (lang && hljs.getLanguage(lang)) {
            return hljs.highlight(code, { language: lang }).value;
        }
        return hljs.highlightAuto(code).value;
    }
}));

marked.setOptions({
    breaks: true,
    gfm: true
});

function renderMarkdown(content) {
    try {
        return marked.parse(content);
    } catch (e) {
        console.error('Markdown parse error:', e);
        return content;
    }
}

// --- Code Execution Blocks ---

function renderCodeExecutionBlocks(activity) {
    // Pair up execute_python_code tool calls with their results
    for (let i = 0; i < activity.length; i++) {
        const item = activity[i];
        if (item.type === 'tool_call' && item.name === 'execute_python_code') {
            const code = item.args?.code || '';
            // Find the matching tool_result
            let output = '';
            for (let j = i + 1; j < activity.length; j++) {
                if (activity[j].type === 'tool_result' && activity[j].name === 'execute_python_code') {
                    output = typeof activity[j].content === 'string' ? activity[j].content : JSON.stringify(activity[j].content, null, 2);
                    break;
                }
            }
            addCodeExecutionBlock(code, output);
        }
    }
}

function addCodeExecutionBlock(code, output) {
    const block = document.createElement('div');
    block.className = 'message assistant';

    const exec = document.createElement('div');
    exec.className = 'code-execution';

    // Code section
    const codeHeader = document.createElement('div');
    codeHeader.className = 'code-execution-header';
    codeHeader.textContent = 'Code';
    exec.appendChild(codeHeader);

    const codePre = document.createElement('pre');
    codePre.className = 'code-execution-code';
    const codeEl = document.createElement('code');
    codeEl.className = 'hljs language-python';
    try {
        codeEl.innerHTML = hljs.highlight(code, { language: 'python' }).value;
    } catch (e) {
        codeEl.textContent = code;
    }
    codePre.appendChild(codeEl);
    exec.appendChild(codePre);

    // Output section
    if (output) {
        const outputHeader = document.createElement('div');
        outputHeader.className = 'code-execution-header';
        outputHeader.textContent = 'Output';
        exec.appendChild(outputHeader);

        const outputPre = document.createElement('pre');
        outputPre.className = 'code-execution-output';
        outputPre.textContent = output;
        exec.appendChild(outputPre);
    }

    block.appendChild(exec);
    messagesDiv.appendChild(block);
    messagesDiv.scrollTop = messagesDiv.scrollHeight;
}

// --- Activity Panel ---

function toggleActivityPanel() {
    activityPanel.classList.toggle('hidden');
    activityToggle.classList.toggle('active');
    localStorage.setItem('activityPanelOpen', !activityPanel.classList.contains('hidden'));
}

activityToggle.addEventListener('click', toggleActivityPanel);
activityClose.addEventListener('click', toggleActivityPanel);

// Restore panel state from localStorage
if (localStorage.getItem('activityPanelOpen') === 'false') {
    activityPanel.classList.add('hidden');
}

function renderActivityItem(item) {
    const itemDiv = document.createElement('div');

    if (item.type === 'thinking') {
        itemDiv.className = 'activity-item thinking';
        const lbl = document.createElement('div');
        lbl.className = 'item-label';
        lbl.textContent = 'Thinking';
        itemDiv.appendChild(lbl);
        const content = document.createElement('div');
        content.className = 'item-content';
        content.textContent = item.content;
        itemDiv.appendChild(content);

    } else if (item.type === 'tool_call') {
        itemDiv.className = 'activity-item tool-call';
        const lbl = document.createElement('div');
        lbl.className = 'item-label';
        lbl.textContent = 'Tool Call';
        itemDiv.appendChild(lbl);
        const name = document.createElement('div');
        name.className = 'item-name';
        name.textContent = item.name;
        itemDiv.appendChild(name);
        if (item.args && Object.keys(item.args).length > 0) {
            const details = document.createElement('details');
            const summary = document.createElement('summary');
            summary.textContent = 'parameters';
            details.appendChild(summary);
            const pre = document.createElement('pre');
            pre.textContent = JSON.stringify(item.args, null, 2);
            details.appendChild(pre);
            itemDiv.appendChild(details);
        }

    } else if (item.type === 'tool_result') {
        itemDiv.className = 'activity-item tool-result';
        const lbl = document.createElement('div');
        lbl.className = 'item-label';
        lbl.textContent = 'Result';
        itemDiv.appendChild(lbl);
        const name = document.createElement('div');
        name.className = 'item-name';
        name.textContent = item.name;
        itemDiv.appendChild(name);
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = 'output';
        details.appendChild(summary);
        const pre = document.createElement('pre');
        pre.textContent = typeof item.content === 'string' ? item.content : JSON.stringify(item.content, null, 2);
        details.appendChild(pre);
        itemDiv.appendChild(details);
    }

    activityContent.appendChild(itemDiv);
    activityContent.scrollTop = activityContent.scrollHeight;
}

// Load activity data embedded by server on page load
const activityDataEl = document.getElementById('activity-data');
if (activityDataEl) {
    try {
        const activityData = JSON.parse(activityDataEl.textContent);
        activityData.forEach(item => renderActivityItem(item));
    } catch (e) {
        console.error('Failed to parse activity data:', e);
    }
}
// Note: Server-rendered code execution blocks are handled via the chat.html template.
// The JS renderCodeExecutionBlocks() is only used for live/dynamic responses.

// --- Messages ---

// Render server-side messages through marked on page load
document.querySelectorAll('.message-content.needs-render').forEach(el => {
    const rawContent = el.textContent;
    if (rawContent) {
        el.innerHTML = renderMarkdown(rawContent);
        el.classList.remove('needs-render');
    }
});

// Auto-resize textarea
if (input) {
    input.addEventListener('input', () => {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 200) + 'px';
    });
}

// Handle form submission
if (form) form.addEventListener('submit', async (e) => {
    e.preventDefault();

    const message = input.value.trim();
    if (!message) return;

    input.disabled = true;
    sendBtn.disabled = true;

    // Clear welcome message if present
    const welcome = messagesDiv.querySelector('.welcome');
    if (welcome) welcome.remove();

    addMessage('user', message);
    input.value = '';
    input.style.height = 'auto';

    const loadingMsg = addMessage('assistant', 'Thinking', true);

    try {
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message: message,
                conversation_id: conversationIdInput.value || null
            })
        });

        if (!response.ok) {
            const errorText = await response.text().catch(() => '');
            throw new Error(`Server error ${response.status}${errorText ? ': ' + errorText : ''}`);
        }

        const data = await response.json();

        // Update conversation ID for new conversations
        if (!conversationIdInput.value) {
            conversationIdInput.value = data.conversation_id;
            history.pushState({}, '', `/c/${data.conversation_id}`);
        }

        loadingMsg.remove();

        // Render code execution blocks inline before the response
        if (data.activity && data.activity.length > 0) {
            renderCodeExecutionBlocks(data.activity);
            data.activity.forEach(item => renderActivityItem(item));
        }

        addMessage('assistant', data.response, false, data.agent_name);

    } catch (error) {
        loadingMsg.remove();
        addMessage('assistant', 'Error: ' + error.message);
    } finally {
        input.disabled = false;
        sendBtn.disabled = false;
        input.focus();
    }
});

function addMessage(role, content, loading = false, agentName = null) {
    const div = document.createElement('div');
    div.className = `message ${role}` + (loading ? ' loading' : '');

    if (agentName) {
        const badge = document.createElement('span');
        badge.className = 'agent-badge';
        badge.textContent = agentName;
        div.appendChild(badge);
    }

    const contentDiv = document.createElement('div');
    contentDiv.className = 'message-content';
    if (loading) {
        contentDiv.textContent = content;
    } else {
        contentDiv.innerHTML = renderMarkdown(content);
    }
    div.appendChild(contentDiv);

    messagesDiv.appendChild(div);
    messagesDiv.scrollTop = messagesDiv.scrollHeight;

    return div;
}

// New chat button
newChatBtn.addEventListener('click', () => {
    window.location.href = '/';
});

// Enter to send, Shift+Enter for newline
if (input) {
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            form.dispatchEvent(new Event('submit'));
        }
    });
}
