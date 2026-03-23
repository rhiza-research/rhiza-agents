import { Widget } from '@lumino/widgets';

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
 */
export class ConversationListWidget extends Widget {
    private _conversations: ConversationData[];
    private _currentId: string;

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
        const nav = document.createElement('nav');
        nav.className = 'conversations';
        for (const conv of this._conversations) {
            const a = document.createElement('a');
            a.href = `/c/${conv.id}`;
            a.className = 'conversation-item';
            if (conv.id === this._currentId) {
                a.classList.add('active');
            }
            a.textContent = conv.title || 'New conversation';
            nav.appendChild(a);
        }
        node.appendChild(nav);
    }
}
