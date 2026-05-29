import { Widget } from '@lumino/widgets';

import { alertDialog, confirmDialog } from '../lib/dialog';

export interface ConversationData {
    id: string;
    title: string | null;
}

interface ConversationListOptions {
    conversations: ConversationData[];
    currentConversationId: string;
    userName: string;
}

/**
 * Sidebar widget showing conversation list, new chat button, and user info.
 *
 * Each row carries a delete affordance. The sidebar lists only the
 * current user's own conversations (server filters by user_id), so the
 * delete request is always against an owned conversation; the backend
 * additionally enforces ownership and returns 403/404 on a mismatch.
 */
export class ConversationListWidget extends Widget {
    private _conversations: ConversationData[];
    private _currentId: string;
    private _nav: HTMLElement | null = null;
    // Conversation IDs with a DELETE request in flight. Guards against a
    // double-click firing a second DELETE (the first removes the row, the
    // second 404s and pops a spurious "Delete failed" alert).
    private _deleting = new Set<string>();

    constructor(options: ConversationListOptions) {
        super();
        this.id = 'conversations';
        this.title.label = 'Chats';
        this.title.closable = true;
        this.addClass('conversations-widget');

        this._conversations = options.conversations;
        this._currentId = options.currentConversationId;
        this._buildDOM();
    }

    private _buildDOM(): void {
        const node = this.node;

        // New chat button
        const header = document.createElement('div');
        header.className = 'sidebar-header';
        const newChatBtn = document.createElement('button');
        newChatBtn.className = 'new-chat-btn';
        newChatBtn.textContent = '+ New Chat';
        newChatBtn.addEventListener('click', () => {
            window.location.href = '/';
        });
        header.appendChild(newChatBtn);
        node.appendChild(header);

        // Conversation list
        this._nav = document.createElement('nav');
        this._nav.className = 'conversations';
        this._renderList();
        node.appendChild(this._nav);
    }

    private _renderList(): void {
        if (!this._nav) return;
        this._nav.innerHTML = '';
        for (const conv of this._conversations) {
            this._nav.appendChild(this._buildRow(conv));
        }
    }

    private _buildRow(conv: ConversationData): HTMLElement {
        const row = document.createElement('div');
        row.className = 'conversation-item';
        row.dataset.conversationId = conv.id;
        if (conv.id === this._currentId) {
            row.classList.add('active');
        }

        const link = document.createElement('a');
        link.href = `/c/${conv.id}`;
        link.className = 'conversation-item-link';
        link.textContent = conv.title || 'New conversation';
        row.appendChild(link);

        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'conversation-delete-btn';
        deleteBtn.title = 'Delete this conversation';
        deleteBtn.setAttribute('aria-label', 'Delete conversation');
        deleteBtn.textContent = '×';
        deleteBtn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            void this._handleDelete(conv, deleteBtn);
        });
        row.appendChild(deleteBtn);

        return row;
    }

    private async _handleDelete(conv: ConversationData, deleteBtn: HTMLButtonElement): Promise<void> {
        // In-flight guard: a rapid second click (before the first request
        // resolves and the row is removed) must not fire a second DELETE.
        if (this._deleting.has(conv.id)) return;

        const title = conv.title || 'this conversation';
        const ok = await confirmDialog({
            title: 'Delete conversation?',
            message:
                `Delete "${title}"? This removes the conversation, its messages, ` +
                'and any files saved on its workspace volume. This cannot be undone.',
            confirmLabel: 'Delete',
            cancelLabel: 'Cancel',
            destructive: true,
        });
        if (!ok) return;

        // Re-check after the await: the user could have confirmed a second
        // dialog opened by a concurrent click before this one resolved.
        if (this._deleting.has(conv.id)) return;
        this._deleting.add(conv.id);
        deleteBtn.disabled = true;

        let response: Response;
        try {
            response = await fetch(`/api/conversations/${conv.id}`, { method: 'DELETE' });
        } catch (e) {
            this._deleting.delete(conv.id);
            deleteBtn.disabled = false;
            await alertDialog({
                title: 'Delete failed',
                message: `Failed to delete: ${(e as Error).message}`,
            });
            return;
        }

        if (!response.ok) {
            this._deleting.delete(conv.id);
            deleteBtn.disabled = false;
            let message = `${response.status} ${response.statusText}`;
            try {
                const data = await response.json();
                if (data?.detail) message = data.detail;
            } catch {
                // ignore JSON parse failure; fall back to status text
            }
            await alertDialog({
                title: 'Delete failed',
                message: `Failed to delete: ${message}`,
            });
            return;
        }

        // Remove from local list and re-render. The row (and its disabled
        // button) is discarded; the _deleting entry is left set since the
        // conversation is gone and cannot be deleted again.
        this._conversations = this._conversations.filter((c) => c.id !== conv.id);
        this._renderList();

        // If the deleted conversation is the one being viewed, navigate home.
        if (conv.id === this._currentId) {
            window.location.href = '/';
        }
    }
}
