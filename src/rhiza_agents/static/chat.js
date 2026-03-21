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
const filesPanel = document.getElementById('files-panel');
const filesList = document.getElementById('files-list');
const fileViewer = document.getElementById('file-viewer');
const fileViewerPath = document.getElementById('file-viewer-path');
const fileViewerCode = document.getElementById('file-viewer-code');
const fileViewerClose = document.getElementById('file-viewer-close');
const fileDownloadBtn = document.getElementById('file-download-btn');
const fileApproveBtn = document.getElementById('file-approve-btn');
const filesToggle = document.getElementById('files-toggle');
const filesClose = document.getElementById('files-close');
const execModeToggle = document.getElementById('exec-mode-toggle');

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

// --- Files Panel ---

function toggleFilesPanel() {
    filesPanel.classList.toggle('hidden');
    filesToggle.classList.toggle('active');
    localStorage.setItem('filesPanelOpen', !filesPanel.classList.contains('hidden'));
    // Refresh file list when opening
    if (!filesPanel.classList.contains('hidden')) {
        loadFiles();
    }
}

filesToggle.addEventListener('click', toggleFilesPanel);
filesClose.addEventListener('click', toggleFilesPanel);

// Restore files panel state
if (localStorage.getItem('filesPanelOpen') === 'true') {
    filesPanel.classList.remove('hidden');
    filesToggle.classList.add('active');
}

// Track the currently viewed file path for download/approve
let currentViewedFilePath = null;
let currentViewedFileContent = null;

async function loadFiles() {
    const convId = conversationIdInput.value;
    if (!convId) {
        filesList.innerHTML = '<div class="files-empty">No conversation selected</div>';
        return;
    }

    try {
        const response = await fetch(`/api/conversations/${convId}/files`);
        if (!response.ok) {
            filesList.innerHTML = '<div class="files-empty">Failed to load files</div>';
            return;
        }

        const files = await response.json();
        if (files.length === 0) {
            filesList.innerHTML = '<div class="files-empty">No files yet</div>';
            filesToggle.classList.remove('has-files');
            return;
        }

        filesToggle.classList.add('has-files');
        filesList.innerHTML = '';
        for (const file of files) {
            const item = document.createElement('div');
            item.className = 'file-item';
            item.addEventListener('click', () => openFile(file.path));

            const pathSpan = document.createElement('span');
            pathSpan.className = 'file-item-path';
            pathSpan.textContent = file.path;
            item.appendChild(pathSpan);

            const meta = document.createElement('span');
            meta.className = 'file-item-meta';
            meta.textContent = formatFileSize(file.size);
            item.appendChild(meta);

            filesList.appendChild(item);
        }
    } catch (e) {
        console.error('Failed to load files:', e);
        filesList.innerHTML = '<div class="files-empty">Error loading files</div>';
    }
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

async function openFile(path) {
    const convId = conversationIdInput.value;
    if (!convId) return;

    try {
        // Strip leading slash for the URL path
        const urlPath = path.startsWith('/') ? path.slice(1) : path;
        const response = await fetch(`/api/conversations/${convId}/files/${urlPath}`);
        if (!response.ok) {
            console.error('Failed to load file:', response.status);
            return;
        }

        const data = await response.json();
        currentViewedFilePath = data.path;
        currentViewedFileContent = data.content;

        fileViewerPath.textContent = data.path;

        // Syntax highlight based on file extension
        const ext = data.path.split('.').pop().toLowerCase();
        const langMap = {
            'py': 'python', 'js': 'javascript', 'ts': 'typescript',
            'json': 'json', 'csv': 'plaintext', 'md': 'markdown',
            'html': 'html', 'css': 'css', 'sql': 'sql',
            'sh': 'bash', 'bash': 'bash', 'yaml': 'yaml', 'yml': 'yaml',
            'txt': 'plaintext', 'xml': 'xml', 'toml': 'toml',
        };
        const lang = langMap[ext] || 'plaintext';

        if (hljs.getLanguage(lang)) {
            fileViewerCode.innerHTML = hljs.highlight(data.content, { language: lang }).value;
        } else {
            fileViewerCode.textContent = data.content;
        }

        // Show approve button for code files in review mode
        const codeExts = ['py', 'js', 'ts', 'sh', 'bash'];
        const isReviewMode = execModeToggle.checked;
        if (codeExts.includes(ext) && isReviewMode) {
            fileApproveBtn.classList.remove('hidden');
        } else {
            fileApproveBtn.classList.add('hidden');
        }

        // Show the viewer, hide the list
        filesList.classList.add('hidden');
        fileViewer.classList.remove('hidden');
    } catch (e) {
        console.error('Failed to open file:', e);
    }
}

fileViewerClose.addEventListener('click', () => {
    fileViewer.classList.add('hidden');
    filesList.classList.remove('hidden');
    currentViewedFilePath = null;
    currentViewedFileContent = null;
});

fileDownloadBtn.addEventListener('click', () => {
    if (!currentViewedFilePath || currentViewedFileContent === null) return;
    const blob = new Blob([currentViewedFileContent], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = currentViewedFilePath.split('/').pop();
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
});

// Approve & Run: send a chat message asking the agent to run the file
fileApproveBtn.addEventListener('click', () => {
    if (!currentViewedFilePath) return;
    const message = `Run the file ${currentViewedFilePath}`;
    input.value = message;
    form.dispatchEvent(new Event('submit'));

    // Close the file viewer back to the list
    fileViewer.classList.add('hidden');
    filesList.classList.remove('hidden');
});

// Execution mode toggle -- persisted in localStorage
if (localStorage.getItem('execReviewMode') === 'true') {
    execModeToggle.checked = true;
}
execModeToggle.addEventListener('change', () => {
    localStorage.setItem('execReviewMode', execModeToggle.checked);
});

// Load files on page load if conversation has files
const hasFilesEl = document.getElementById('has-files-data');
if (hasFilesEl) {
    try {
        const hasFiles = JSON.parse(hasFilesEl.textContent);
        if (hasFiles) {
            loadFiles();
        }
    } catch (e) {
        // ignore
    }
}

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

// --- SSE Streaming ---

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
            } catch (e) { /* keep as string */ }
            parsed.push(currentEvent);
            currentEvent = null;
        }
    }

    // Keep unparsed data in buffer
    if (currentEvent) {
        const dataStr = typeof currentEvent.data === 'string'
            ? currentEvent.data
            : JSON.stringify(currentEvent.data);
        remaining = `event: ${currentEvent.type}\ndata: ${dataStr}\n`;
    }

    return { parsed, remaining };
}

let streamedContent = '';
let activeAbortController = null;

function addStreamingMessage() {
    streamedContent = '';
    const div = document.createElement('div');
    div.className = 'message assistant streaming';
    div.innerHTML = `
        <div class="agent-badge-container"></div>
        <div class="message-content"></div>
    `;
    messagesDiv.appendChild(div);
    messagesDiv.scrollTop = messagesDiv.scrollHeight;
    return div;
}

function appendToken(msgDiv, token) {
    if (!token) return;
    streamedContent += token;
    const contentDiv = msgDiv.querySelector('.message-content');
    contentDiv.innerHTML = renderMarkdown(streamedContent);
    messagesDiv.scrollTop = messagesDiv.scrollHeight;
}

function updateAgentBadge(msgDiv, agentName) {
    if (!agentName) return;
    const container = msgDiv.querySelector('.agent-badge-container');
    const escaped = document.createElement('span');
    escaped.textContent = agentName;
    container.innerHTML = `<span class="agent-badge">${escaped.innerHTML}</span>`;
}

function finalizeMessage(msgDiv) {
    msgDiv.classList.remove('streaming');
    streamedContent = '';
}

function setMessageError(msgDiv, error) {
    msgDiv.classList.remove('streaming');
    const contentDiv = msgDiv.querySelector('.message-content');
    contentDiv.textContent = 'Error: ' + error;
    streamedContent = '';
}

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

        case 'thinking': {
            // Append to the latest thinking item if one exists,
            // otherwise create a new one
            const lastItem = activityContent.lastElementChild;
            if (lastItem && lastItem.classList.contains('thinking')) {
                const contentEl = lastItem.querySelector('.item-content');
                if (contentEl) {
                    contentEl.textContent += event.data.content;
                    activityContent.scrollTop = activityContent.scrollHeight;
                    break;
                }
            }
            renderActivityItem({type: 'thinking', content: event.data.content});
            break;
        }

        case 'tool_start':
            renderActivityItem({type: 'tool_call', name: event.data.name, args: event.data.input});
            break;

        case 'tool_end':
            renderActivityItem({type: 'tool_result', name: event.data.name, content: event.data.output});
            break;

        case 'files_changed':
            // Refresh file list when a file is written
            loadFiles();
            filesToggle.classList.add('has-files');
            break;

        case 'interrupt':
            renderInterruptCard(event.data, msgDiv);
            break;

        case 'error':
            setMessageError(msgDiv, event.data.error);
            break;

        case 'done':
            finalizeMessage(msgDiv);
            break;
    }
}

function renderInterruptCard(interruptData, msgDiv) {
    // Show an approval card for HITL interrupts
    const card = document.createElement('div');
    card.className = 'interrupt-card';

    const actionRequests = interruptData.action_requests || [];
    const toolName = actionRequests.length > 0 ? actionRequests[0].action?.name : 'Unknown tool';
    const toolArgs = actionRequests.length > 0 ? actionRequests[0].action?.args : {};

    card.innerHTML = `
        <div class="interrupt-header">Approval Required</div>
        <div class="interrupt-tool">${toolName}</div>
        <details class="interrupt-args">
            <summary>arguments</summary>
            <pre>${JSON.stringify(toolArgs, null, 2)}</pre>
        </details>
        <div class="interrupt-actions">
            <button class="interrupt-approve">Approve</button>
            <button class="interrupt-reject">Reject</button>
        </div>
    `;

    card.querySelector('.interrupt-approve').addEventListener('click', () => {
        card.remove();
        resumeExecution('approve', null, msgDiv);
    });

    card.querySelector('.interrupt-reject').addEventListener('click', () => {
        card.remove();
        resumeExecution('reject', 'Rejected by user', msgDiv);
    });

    messagesDiv.appendChild(card);
    messagesDiv.scrollTop = messagesDiv.scrollHeight;
}

async function resumeExecution(decision, message, msgDiv) {
    const convId = conversationIdInput.value;
    if (!convId) return;

    // Reuse or create a streaming message div
    if (!msgDiv || !msgDiv.classList.contains('streaming')) {
        msgDiv = addStreamingMessage();
    }

    try {
        const response = await fetch('/api/chat/resume', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                conversation_id: convId,
                decision: decision,
                message: message,
            }),
        });

        if (!response.ok) {
            setMessageError(msgDiv, `Resume failed: ${response.status}`);
            return;
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const events = parseSSEEvents(buffer);
            buffer = events.remaining;

            for (const event of events.parsed) {
                handleStreamEvent(event, msgDiv);
            }
        }

        if (msgDiv.classList.contains('streaming')) {
            finalizeMessage(msgDiv);
        }
    } catch (error) {
        setMessageError(msgDiv, error.message);
    }
}

// Handle form submission with streaming
if (form) form.addEventListener('submit', async (e) => {
    e.preventDefault();

    const message = input.value.trim();
    if (!message) return;

    // Cancel any active stream
    if (activeAbortController) {
        activeAbortController.abort();
    }
    activeAbortController = new AbortController();

    input.disabled = true;
    sendBtn.disabled = true;

    // Clear welcome message if present
    const welcome = messagesDiv.querySelector('.welcome');
    if (welcome) welcome.remove();

    addMessage('user', message);
    input.value = '';
    input.style.height = 'auto';

    const assistantMsg = addStreamingMessage();

    try {
        const response = await fetch('/api/chat/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message: message,
                conversation_id: conversationIdInput.value || null,
                execution_mode: execModeToggle.checked ? 'review' : 'auto',
            }),
            signal: activeAbortController.signal,
        });

        if (!response.ok) {
            const errorText = await response.text().catch(() => '');
            throw new Error(`Server error ${response.status}${errorText ? ': ' + errorText : ''}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const events = parseSSEEvents(buffer);
            buffer = events.remaining;

            for (const event of events.parsed) {
                handleStreamEvent(event, assistantMsg);
            }
        }

        // Ensure finalized even if no done event
        if (assistantMsg.classList.contains('streaming')) {
            finalizeMessage(assistantMsg);
        }

    } catch (error) {
        if (error.name === 'AbortError') {
            finalizeMessage(assistantMsg);
        } else {
            setMessageError(assistantMsg, error.message);
        }
    } finally {
        activeAbortController = null;
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

// Sample prompt buttons
document.querySelectorAll('.sample-prompt').forEach(btn => {
    btn.addEventListener('click', () => {
        input.value = btn.dataset.prompt;
        form.dispatchEvent(new Event('submit'));
    });
});

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
