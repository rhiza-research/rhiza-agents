import { Widget } from '@lumino/widgets';
import { hljs, renderMarkdown } from '../lib/markdown';

const LANG_MAP: Record<string, string> = {
    'py': 'python', 'js': 'javascript', 'ts': 'typescript',
    'json': 'json', 'csv': 'plaintext', 'md': 'markdown',
    'html': 'html', 'css': 'css', 'sql': 'sql',
    'sh': 'bash', 'bash': 'bash', 'yaml': 'yaml', 'yml': 'yaml',
    'txt': 'plaintext', 'xml': 'xml', 'toml': 'toml',
};

/**
 * A tab widget that displays a single file's content with syntax highlighting.
 * Markdown files get a toggle between raw and rendered views.
 */
export class FileViewerWidget extends Widget {
    private _path: string;
    private _content: string;
    private _source: string;
    private _isMarkdown: boolean;
    private _showRendered = true;
    private _contentContainer!: HTMLDivElement;
    private _toggleBtn: HTMLButtonElement | null = null;

    constructor(path: string, content: string, source: string = 'agent') {
        super();
        this._path = path;
        this._content = content;
        this._source = source;
        this._isMarkdown = path.toLowerCase().endsWith('.md');

        const filename = path.split('/').pop() || path;
        this.id = 'file-' + path.replace(/[^a-zA-Z0-9]/g, '_');
        this.title.label = filename;
        this.title.closable = true;
        this.addClass('file-viewer-widget');

        this._buildDOM();
    }

    get filePath(): string { return this._path; }

    private _buildDOM(): void {
        const header = document.createElement('div');
        header.className = 'file-viewer-header';

        const pathEl = document.createElement('span');
        pathEl.className = 'file-viewer-path';
        pathEl.textContent = this._path;
        header.appendChild(pathEl);

        const sourceTag = document.createElement('span');
        sourceTag.className = `file-source file-source-${this._source}`;
        sourceTag.textContent = this._source === 'output' ? 'code-generated' : 'agent-generated';
        header.appendChild(sourceTag);

        const actions = document.createElement('div');
        actions.className = 'file-viewer-actions';

        if (this._isMarkdown) {
            this._toggleBtn = document.createElement('button');
            this._toggleBtn.className = 'file-download-btn';
            this._toggleBtn.textContent = 'Raw';
            this._toggleBtn.addEventListener('click', () => this._toggleView());
            actions.appendChild(this._toggleBtn);
        }

        const downloadBtn = document.createElement('button');
        downloadBtn.className = 'file-download-btn';
        downloadBtn.textContent = 'Download';
        downloadBtn.addEventListener('click', () => this._download());
        actions.appendChild(downloadBtn);

        header.appendChild(actions);
        this.node.appendChild(header);

        this._contentContainer = document.createElement('div');
        this._contentContainer.style.flex = '1';
        this._contentContainer.style.overflow = 'auto';
        this.node.appendChild(this._contentContainer);

        this._renderContent();
    }

    private _renderContent(): void {
        this._contentContainer.innerHTML = '';

        if (this._isMarkdown && this._showRendered) {
            const rendered = document.createElement('div');
            rendered.className = 'message-content';
            rendered.style.padding = '1rem';
            rendered.innerHTML = renderMarkdown(this._content);
            this._contentContainer.appendChild(rendered);
        } else {
            const pre = document.createElement('pre');
            pre.className = 'file-viewer-content';
            pre.style.margin = '0';
            pre.style.height = '100%';
            const code = document.createElement('code');

            const ext = this._path.split('.').pop()?.toLowerCase() || '';
            const lang = LANG_MAP[ext] || 'plaintext';
            if (hljs.getLanguage(lang)) {
                code.innerHTML = hljs.highlight(this._content, { language: lang }).value;
            } else {
                code.textContent = this._content;
            }

            pre.appendChild(code);
            this._contentContainer.appendChild(pre);
        }
    }

    private _toggleView(): void {
        this._showRendered = !this._showRendered;
        if (this._toggleBtn) {
            this._toggleBtn.textContent = this._showRendered ? 'Raw' : 'Rendered';
        }
        this._renderContent();
    }

    private _download(): void {
        const blob = new Blob([this._content], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = this._path.split('/').pop() || 'file';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    }
}
