import { Widget } from '@lumino/widgets';
import { renderMarkdown } from '../lib/markdown';
import { parseSSEEvents } from '../lib/sse';
import { ActivityWidget } from './activity';
import { FilesWidget } from './files';

interface ChatWidgetOptions {
    conversationId: string;
    activityWidget: ActivityWidget;
    filesWidget: FilesWidget;
}

/**
 * Main chat widget — message display, input form, SSE streaming.
 */
export class ChatWidget extends Widget {
    private _conversationId: string;
    private _activity: ActivityWidget;
    private _files: FilesWidget;
    private _streamedContent = '';
    private _activeAbortController: AbortController | null = null;
    private _reviewMode = false;
    private _currentStreamingDiv: HTMLDivElement | null = null;
    private _currentAgent: string | null = null;
    private _currentTraceId: string | null = null;

    private _messagesDiv!: HTMLDivElement;
    private _input!: HTMLTextAreaElement;
    private _sendBtn!: HTMLButtonElement;
    private _execToggle!: HTMLInputElement;

    constructor(options: ChatWidgetOptions) {
        super();
        this.id = 'chat';
        this.title.label = 'Chat';
        this.title.closable = false;
        this.addClass('chat-widget');

        this._conversationId = options.conversationId;
        this._activity = options.activityWidget;
        this._files = options.filesWidget;

        if (localStorage.getItem('execReviewMode') === 'true') {
            this._reviewMode = true;
        }

        this._buildDOM();
        this._loadMessages();
    }

    get conversationId(): string { return this._conversationId; }
    get reviewMode(): boolean { return this._reviewMode; }

    private _buildDOM(): void {
        const node = this.node;

        // Header with review mode toggle
        const header = document.createElement('div');
        header.className = 'chat-header';
        const label = document.createElement('label');
        label.className = 'exec-mode-toggle';
        label.title = 'When enabled, code must be approved before execution';
        this._execToggle = document.createElement('input');
        this._execToggle.type = 'checkbox';
        this._execToggle.checked = this._reviewMode;
        this._execToggle.addEventListener('change', () => {
            this._reviewMode = this._execToggle.checked;
            localStorage.setItem('execReviewMode', String(this._reviewMode));
        });
        label.appendChild(this._execToggle);
        const span = document.createElement('span');
        span.className = 'exec-mode-label';
        span.textContent = 'Review code';
        label.appendChild(span);
        header.appendChild(label);
        node.appendChild(header);

        // Messages area
        this._messagesDiv = document.createElement('div');
        this._messagesDiv.className = 'messages';

        if (!this._conversationId) {
            this._messagesDiv.innerHTML = `
                <div class="welcome">
                    <h2>Rhiza Agents</h2>
                    <p>Ask questions about weather forecast models and data analysis.</p>
                    <div class="sample-prompts">
                        <button class="sample-prompt" data-prompt="What metrics are available in sheerwater?">What metrics are available in sheerwater?</button>
                        <button class="sample-prompt" data-prompt="Can you write a script that generates a hash of the word 'sheerwater' and saves the file to disk?">Write a script to hash \u201csheerwater\u201d and save to file</button>
                        <button class="sample-prompt" data-prompt="Can you show a map of Africa?">Show a map of Africa</button>
                    </div>
                </div>
            `;
        } else {
            this._messagesDiv.innerHTML = '<div class="messages-loading">Loading messages...</div>';
        }
        node.appendChild(this._messagesDiv);

        // Input form
        const form = document.createElement('form');
        form.className = 'input-area';
        this._input = document.createElement('textarea');
        this._input.placeholder = 'Ask about forecast models...';
        this._input.rows = 1;
        this._input.addEventListener('input', () => {
            this._input.style.height = 'auto';
            this._input.style.height = Math.min(this._input.scrollHeight, 200) + 'px';
        });
        this._input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this._handleSubmit();
            }
        });
        form.appendChild(this._input);

        this._sendBtn = document.createElement('button');
        this._sendBtn.type = 'button';
        this._sendBtn.textContent = 'Send';
        this._sendBtn.addEventListener('click', () => this._handleSubmit());
        form.appendChild(this._sendBtn);

        node.appendChild(form);

        // Sample prompt click handlers
        this._messagesDiv.addEventListener('click', (e) => {
            const btn = (e.target as HTMLElement).closest('.sample-prompt') as HTMLElement | null;
            if (btn) {
                this._input.value = btn.dataset.prompt || '';
                this._handleSubmit();
            }
        });
    }

    private async _loadMessages(): Promise<void> {
        if (!this._conversationId) return;

        try {
            const response = await fetch(`/api/conversations/${this._conversationId}/messages`);
            if (!response.ok) {
                console.error('Failed to load messages:', response.status);
                return;
            }

            const data = await response.json();
            const messages: any[] = data.messages || [];

            const loading = this._messagesDiv.querySelector('.messages-loading');
            if (loading) loading.remove();

            let hasFiles = false;
            for (const msg of messages) {
                if (msg.type === 'human') {
                    this._addMessage('user', msg.content);
                } else if (msg.type === 'ai') {
                    this._addMessage('assistant', msg.content, false, msg.agent_name);
                } else if (msg.type === 'chart') {
                    this._addChart(msg.url);
                } else if (msg.type === 'thinking') {
                    this._activity.addItem({ type: 'thinking', content: msg.content });
                } else if (msg.type === 'tool_call') {
                    this._activity.addItem({ type: 'tool_call', name: msg.name, args: msg.args });
                    if (msg.name === 'write_file' || msg.name === 'run_file') {
                        hasFiles = true;
                    }
                } else if (msg.type === 'tool_result') {
                    this._activity.addItem({ type: 'tool_result', name: msg.name, content: msg.content });
                }
            }

            this._messagesDiv.scrollTop = this._messagesDiv.scrollHeight;
            if (hasFiles) this._files.loadFiles();
        } catch (e) {
            console.error('Failed to load messages:', e);
            const loading = this._messagesDiv.querySelector('.messages-loading');
            if (loading) loading.textContent = 'Failed to load messages';
        }
    }

    private _addMessage(role: string, content: string, loading = false, agentName?: string): HTMLDivElement {
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
        contentDiv.innerHTML = loading ? content : renderMarkdown(content);
        div.appendChild(contentDiv);

        this._messagesDiv.appendChild(div);
        this._messagesDiv.scrollTop = this._messagesDiv.scrollHeight;
        return div;
    }

    private _addChart(url: string): void {
        const div = document.createElement('div');
        div.className = 'message assistant';
        const iframe = document.createElement('iframe');
        iframe.src = url;
        iframe.className = 'chart-iframe';
        iframe.style.cssText = 'width:100%;height:500px;border:none;border-radius:8px;';
        const contentDiv = document.createElement('div');
        contentDiv.className = 'message-content';
        contentDiv.appendChild(iframe);
        div.appendChild(contentDiv);
        this._messagesDiv.appendChild(div);
    }

    // --- Streaming ---

    private _addStreamingMessage(): HTMLDivElement {
        this._streamedContent = '';
        const div = document.createElement('div');
        div.className = 'message assistant streaming';
        div.innerHTML = `
            <div class="agent-badge-container"></div>
            <div class="message-content"></div>
        `;
        if (this._currentTraceId) {
            div.dataset.traceId = this._currentTraceId;
        }
        this._messagesDiv.appendChild(div);
        this._messagesDiv.scrollTop = this._messagesDiv.scrollHeight;
        return div;
    }

    private _attachFeedbackButtons(msgDiv: HTMLDivElement): void {
        const traceId = msgDiv.dataset.traceId;
        if (!traceId) return;
        if (msgDiv.querySelector('.feedback-buttons')) return;

        const wrap = document.createElement('div');
        wrap.className = 'feedback-buttons';
        wrap.innerHTML = `
            <button class="feedback-btn feedback-up" title="This response was helpful">&#x1F44D;</button>
            <button class="feedback-btn feedback-down" title="This response was not helpful">&#x1F44E;</button>
            <span class="feedback-status" aria-live="polite"></span>
        `;
        const status = wrap.querySelector('.feedback-status') as HTMLSpanElement;

        const submit = async (value: 1 | -1, btn: HTMLButtonElement) => {
            const buttons = wrap.querySelectorAll<HTMLButtonElement>('.feedback-btn');
            buttons.forEach((b) => (b.disabled = true));
            try {
                const res = await fetch('/api/chat/feedback', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ trace_id: traceId, value }),
                });
                if (!res.ok) {
                    throw new Error(`HTTP ${res.status}`);
                }
                btn.classList.add('feedback-active');
                status.textContent = 'Thanks!';
            } catch (err: any) {
                buttons.forEach((b) => (b.disabled = false));
                status.textContent = `Failed: ${err.message}`;
            }
        };

        wrap.querySelector('.feedback-up')!.addEventListener('click', (e) =>
            submit(1, e.currentTarget as HTMLButtonElement),
        );
        wrap.querySelector('.feedback-down')!.addEventListener('click', (e) =>
            submit(-1, e.currentTarget as HTMLButtonElement),
        );
        msgDiv.appendChild(wrap);
    }

    private _appendToken(msgDiv: HTMLDivElement, token: string): void {
        if (!token) return;
        this._streamedContent += token;
        const contentDiv = msgDiv.querySelector('.message-content')!;
        contentDiv.innerHTML = renderMarkdown(this._streamedContent);
        this._messagesDiv.scrollTop = this._messagesDiv.scrollHeight;
    }

    private _updateAgentBadge(msgDiv: HTMLDivElement, agentName: string): void {
        if (!agentName) return;
        const container = msgDiv.querySelector('.agent-badge-container')!;
        const escaped = document.createElement('span');
        escaped.textContent = agentName;
        container.innerHTML = `<span class="agent-badge">${escaped.innerHTML}</span>`;
    }

    private _appendChart(msgDiv: HTMLDivElement, url: string): void {
        const iframe = document.createElement('iframe');
        iframe.src = url;
        iframe.className = 'chart-iframe';
        iframe.style.cssText = 'width:100%;height:500px;border:none;border-radius:8px;margin-top:8px;';
        msgDiv.appendChild(iframe);
        this._messagesDiv.scrollTop = this._messagesDiv.scrollHeight;
    }

    private _finalizeMessage(msgDiv: HTMLDivElement): void {
        msgDiv.classList.remove('streaming');
        this._streamedContent = '';
        this._attachFeedbackButtons(msgDiv);
    }

    private _setMessageError(msgDiv: HTMLDivElement, error: string): void {
        msgDiv.classList.remove('streaming');
        const contentDiv = msgDiv.querySelector('.message-content')!;
        contentDiv.textContent = 'Error: ' + error;
        this._streamedContent = '';
    }

    private _handleStreamEvent(event: any): void {
        const msgDiv = this._currentStreamingDiv!;
        switch (event.type) {
            case 'conversation_id':
                if (!this._conversationId) {
                    this._conversationId = event.data.conversation_id;
                    history.pushState({}, '', `/c/${event.data.conversation_id}`);
                }
                break;
            case 'trace_id':
                this._currentTraceId = event.data.trace_id;
                if (this._currentStreamingDiv) {
                    this._currentStreamingDiv.dataset.traceId = event.data.trace_id;
                }
                break;
            case 'agent_start': {
                const newAgent = event.data.agent;
                if (this._currentAgent && newAgent !== this._currentAgent) {
                    const cur = this._currentStreamingDiv!;
                    if (this._streamedContent) {
                        // Current bubble has content — finalize it
                        this._finalizeMessage(cur);
                    } else {
                        // Empty bubble — remove it
                        cur.remove();
                    }
                    this._currentStreamingDiv = this._addStreamingMessage();
                }
                this._currentAgent = newAgent;
                this._updateAgentBadge(this._currentStreamingDiv!, newAgent);
                break;
            }
            case 'token':
                this._appendToken(this._currentStreamingDiv!, event.data.content);
                break;
            case 'thinking':
                this._activity.appendThinking(event.data.content);
                break;
            case 'tool_start':
                this._activity.addItem({ type: 'tool_call', name: event.data.name, args: event.data.input });
                break;
            case 'tool_end':
                this._activity.addItem({ type: 'tool_result', name: event.data.name, content: event.data.output });
                if (event.data.name === 'run_file') this._files.loadFiles();
                break;
            case 'chart':
                this._appendChart(this._currentStreamingDiv!, event.data.url);
                break;
            case 'file_written':
                this._files.addStreamedFile(event.data.path, event.data.content);
                break;
            case 'files_changed':
                this._files.loadFiles();
                break;
            case 'interrupt':
                this._renderInterruptCard(event.data);
                break;
            case 'error':
                this._setMessageError(this._currentStreamingDiv!, event.data.error);
                break;
            case 'done':
                this._finalizeMessage(this._currentStreamingDiv!);
                this._currentStreamingDiv = null;
                this._files.loadFiles();
                break;
        }
    }

    private _renderInterruptCard(interruptData: any): void {
        const card = document.createElement('div');
        card.className = 'interrupt-card';

        const actionRequests = interruptData.action_requests || [];
        const toolName = actionRequests.length > 0
            ? (actionRequests[0].name || actionRequests[0].action?.name || 'Unknown tool')
            : 'Unknown tool';
        const toolArgs = actionRequests.length > 0
            ? (actionRequests[0].args || actionRequests[0].action?.args || {})
            : {};

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

        card.querySelector('.interrupt-approve')!.addEventListener('click', () => {
            card.remove();
            this._resumeExecution('approve', null);
        });
        card.querySelector('.interrupt-reject')!.addEventListener('click', () => {
            card.remove();
            this._resumeExecution('reject', 'Rejected by user');
        });

        this._messagesDiv.appendChild(card);
        this._messagesDiv.scrollTop = this._messagesDiv.scrollHeight;
    }

    private async _resumeExecution(decision: string, message: string | null): Promise<void> {
        if (!this._conversationId) return;

        this._currentTraceId = null;
        this._currentStreamingDiv = this._addStreamingMessage();
        this._currentAgent = null;

        try {
            const response = await fetch('/api/chat/resume', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    conversation_id: this._conversationId,
                    decision,
                    message,
                }),
            });

            if (!response.ok) {
                this._setMessageError(this._currentStreamingDiv!, `Resume failed: ${response.status}`);
                return;
            }

            const reader = response.body!.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const events = parseSSEEvents(buffer);
                buffer = events.remaining;
                for (const event of events.parsed) {
                    this._handleStreamEvent(event);
                }
            }

            if (this._currentStreamingDiv?.classList.contains('streaming')) {
                this._finalizeMessage(this._currentStreamingDiv);
                this._currentStreamingDiv = null;
            }
        } catch (error: any) {
            if (this._currentStreamingDiv) {
                this._setMessageError(this._currentStreamingDiv, error.message);
            }
        }
    }

    private async _handleSubmit(): Promise<void> {
        const message = this._input.value.trim();
        if (!message) return;

        if (this._activeAbortController) {
            this._activeAbortController.abort();
        }
        this._activeAbortController = new AbortController();

        this._input.disabled = true;
        this._sendBtn.disabled = true;

        const welcome = this._messagesDiv.querySelector('.welcome');
        if (welcome) welcome.remove();

        this._addMessage('user', message);
        this._input.value = '';
        this._input.style.height = 'auto';

        this._currentTraceId = null;
        this._currentStreamingDiv = this._addStreamingMessage();
        this._currentAgent = null;

        try {
            const response = await fetch('/api/chat/stream', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message,
                    conversation_id: this._conversationId || null,
                    execution_mode: this._reviewMode ? 'review' : 'auto',
                }),
                signal: this._activeAbortController.signal,
            });

            if (!response.ok) {
                const errorText = await response.text().catch(() => '');
                throw new Error(`Server error ${response.status}${errorText ? ': ' + errorText : ''}`);
            }

            const reader = response.body!.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const events = parseSSEEvents(buffer);
                buffer = events.remaining;
                for (const event of events.parsed) {
                    this._handleStreamEvent(event);
                }
            }

            if (this._currentStreamingDiv?.classList.contains('streaming')) {
                this._finalizeMessage(this._currentStreamingDiv);
                this._currentStreamingDiv = null;
            }
        } catch (error: any) {
            if (error.name === 'AbortError') {
                if (this._currentStreamingDiv) this._finalizeMessage(this._currentStreamingDiv);
            } else {
                if (this._currentStreamingDiv) this._setMessageError(this._currentStreamingDiv, error.message);
            }
            this._currentStreamingDiv = null;
        } finally {
            this._activeAbortController = null;
            this._input.disabled = false;
            this._sendBtn.disabled = false;
            this._input.focus();
        }
    }

    sendMessage(text: string): void {
        this._input.value = text;
        this._handleSubmit();
    }
}
