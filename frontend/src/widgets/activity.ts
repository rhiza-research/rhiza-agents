import { Widget } from '@lumino/widgets';

export interface ActivityItem {
    type: 'thinking' | 'tool_call' | 'tool_result';
    content?: string;
    name?: string;
    args?: Record<string, any>;
}

/**
 * Activity panel widget showing thinking, tool calls, and results.
 */
export class ActivityWidget extends Widget {
    private _content: HTMLDivElement;

    constructor() {
        super();
        this.id = 'activity';
        this.title.label = 'Activity';
        this.title.closable = true;
        this.addClass('activity-widget');

        this._content = document.createElement('div');
        this._content.className = 'activity-content';
        this.node.appendChild(this._content);
    }

    addItem(item: ActivityItem): void {
        const itemDiv = document.createElement('div');

        if (item.type === 'thinking') {
            itemDiv.className = 'activity-item thinking';
            const lbl = document.createElement('div');
            lbl.className = 'item-label';
            lbl.textContent = 'Thinking';
            itemDiv.appendChild(lbl);
            const content = document.createElement('div');
            content.className = 'item-content';
            content.textContent = item.content || '';
            itemDiv.appendChild(content);

        } else if (item.type === 'tool_call') {
            itemDiv.className = 'activity-item tool-call';
            const lbl = document.createElement('div');
            lbl.className = 'item-label';
            lbl.textContent = 'Tool Call';
            itemDiv.appendChild(lbl);
            const name = document.createElement('div');
            name.className = 'item-name';
            name.textContent = item.name || '';
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
            name.textContent = item.name || '';
            itemDiv.appendChild(name);
            const details = document.createElement('details');
            const summary = document.createElement('summary');
            summary.textContent = 'output';
            details.appendChild(summary);
            const pre = document.createElement('pre');
            pre.textContent = typeof item.content === 'string'
                ? item.content
                : JSON.stringify(item.content, null, 2);
            details.appendChild(pre);
            itemDiv.appendChild(details);
        }

        this._content.appendChild(itemDiv);
        this._content.scrollTop = this._content.scrollHeight;
    }

    appendThinking(content: string): void {
        const lastItem = this._content.lastElementChild;
        if (lastItem && lastItem.classList.contains('thinking')) {
            const contentEl = lastItem.querySelector('.item-content');
            if (contentEl) {
                contentEl.textContent += content;
                this._content.scrollTop = this._content.scrollHeight;
                return;
            }
        }
        this.addItem({ type: 'thinking', content });
    }

    clear(): void {
        this._content.innerHTML = '';
    }
}
