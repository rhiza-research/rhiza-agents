import { Widget } from '@lumino/widgets';

function formatFileSize(bytes: number): string {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

interface FilesWidgetOptions {
    getConversationId: () => string;
    getReviewMode: () => boolean;
    onRunFile: (path: string) => void;
    onOpenFile: (path: string, content: string, source: string) => void;
}

/**
 * Files panel widget — file list + file viewer with download/approve.
 */
export class FilesWidget extends Widget {
    private _getConversationId: () => string;
    private _getReviewMode: () => boolean;
    private _onRunFile: (path: string) => void;
    private _onOpenFile: (path: string, content: string, source: string) => void;

    private _streamedFiles: Record<string, string> = {};
    private _fileSources: Record<string, string> = {};
    private _filesList!: HTMLDivElement;

    constructor(options: FilesWidgetOptions) {
        super();
        this.id = 'files';
        this.title.label = 'Files';
        this.title.closable = true;
        this.addClass('files-widget');

        this._getConversationId = options.getConversationId;
        this._getReviewMode = options.getReviewMode;
        this._onRunFile = options.onRunFile;
        this._onOpenFile = options.onOpenFile;

        this._buildDOM();
    }

    private _buildDOM(): void {
        const node = this.node;

        this._filesList = document.createElement('div');
        this._filesList.className = 'files-list';
        node.appendChild(this._filesList);
    }

    async loadFiles(): Promise<void> {
        const convId = this._getConversationId();
        if (!convId) {
            this._filesList.innerHTML = '<div class="files-empty">No conversation selected</div>';
            return;
        }

        try {
            const response = await fetch(`/api/conversations/${convId}/files`);
            if (!response.ok) {
                this._filesList.innerHTML = '<div class="files-empty">Failed to load files</div>';
                return;
            }

            const files: any[] = await response.json();
            const apiPaths = new Set(files.map((f: any) => f.path));
            for (const [path, content] of Object.entries(this._streamedFiles)) {
                if (!apiPaths.has(path)) {
                    files.push({ path, size: new Blob([content || '']).size });
                }
            }
            if (files.length === 0) {
                this._filesList.innerHTML = '<div class="files-empty">No files yet</div>';
                return;
            }

            this._filesList.innerHTML = '';
            for (const file of files) {
                const source = file.source || 'agent';
                this._fileSources[file.path] = source;
                this._addFileEntry(file.path, file.size, source);
            }
        } catch (e) {
            console.error('Failed to load files:', e);
            this._filesList.innerHTML = '<div class="files-empty">Error loading files</div>';
        }
    }

    addStreamedFile(path: string, content: string): void {
        if (!path) return;
        if (!path.startsWith('/')) path = '/' + path;
        this._streamedFiles[path] = content;
        this._fileSources[path] = 'agent';

        this.show();

        const empty = this._filesList.querySelector('.files-empty');
        if (empty) empty.remove();

        const existing = this._filesList.querySelector(`[data-path="${path}"]`);
        if (existing) {
            const meta = existing.querySelector('.file-item-meta');
            if (meta) meta.textContent = formatFileSize(new Blob([content]).size);
            return;
        }

        this._addFileEntry(path, new Blob([content]).size, 'agent');
    }

    get hasFiles(): boolean {
        return this._filesList.querySelector('.file-item') !== null;
    }

    private _addFileEntry(path: string, size: number, source: string): void {
        const item = document.createElement('div');
        item.className = 'file-item';
        item.dataset.path = path;
        item.addEventListener('click', () => this._openFile(path));

        const pathSpan = document.createElement('span');
        pathSpan.className = 'file-item-path';
        pathSpan.textContent = path;
        item.appendChild(pathSpan);

        const sourceLabel = document.createElement('span');
        sourceLabel.className = `file-source file-source-${source}`;
        sourceLabel.textContent = source === 'output' ? 'code-generated' : 'agent-generated';
        item.appendChild(sourceLabel);

        const meta = document.createElement('span');
        meta.className = 'file-item-meta';
        meta.textContent = formatFileSize(size);
        item.appendChild(meta);

        this._filesList.appendChild(item);
    }

    private async _openFile(path: string): Promise<void> {
        // Check streamed cache first
        const cached = this._streamedFiles[path];
        if (cached !== undefined) {
            this._onOpenFile(path, cached, this._fileSources[path] || 'agent');
            return;
        }

        const convId = this._getConversationId();
        if (!convId) return;

        try {
            const urlPath = path.startsWith('/') ? path.slice(1) : path;
            const response = await fetch(`/api/conversations/${convId}/files/${urlPath}`);
            if (!response.ok) {
                console.error('Failed to load file:', response.status);
                return;
            }
            const data = await response.json();
            this._onOpenFile(data.path, data.content, this._fileSources[path] || data.source || 'agent');
        } catch (e) {
            console.error('Failed to open file:', e);
        }
    }
}
