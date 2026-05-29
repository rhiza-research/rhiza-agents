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
    onOpenFile: (path: string, content: string, source: string, encoding?: string) => void;
}

type FileScope = 'session' | 'workspace';

/**
 * Files panel widget — file list + file viewer with download/approve.
 *
 * Two scopes:
 * - session: files this conversation tracked in state (offline-friendly,
 *   rendered from LangGraph checkpoint data; works without an active sandbox)
 * - workspace: live filesystem listing of /workspace (lazy-creates the
 *   sandbox if none is running; cold-start latency is on this click)
 *
 * File contents are fetched on demand from the live sandbox; clicking a
 * file in either scope triggers sandbox creation if needed.
 */
export class FilesWidget extends Widget {
    private _getConversationId: () => string;
    private _getReviewMode: () => boolean;
    private _onRunFile: (path: string) => void;
    private _onOpenFile: (path: string, content: string, source: string, encoding?: string) => void;

    private _streamedFiles: Record<string, string> = {};
    private _fileSources: Record<string, string> = {};
    private _fileEncodings: Record<string, string> = {};
    private _filesList!: HTMLDivElement;
    private _scopeToggle!: HTMLDivElement;
    private _sessionBtn!: HTMLButtonElement;
    private _workspaceBtn!: HTMLButtonElement;
    private _scope: FileScope = 'session';
    // Monotonic token identifying the most recent loadFiles call. A slow
    // response (e.g. the workspace listing that lazy-starts a sandbox) must
    // not overwrite the list after the user toggled back to session and a
    // newer load already ran. Each load captures the token at issue time
    // and discards its result if a newer load has since started.
    private _loadToken = 0;

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

        // Scope toggle: session (default) vs workspace (live FS)
        this._scopeToggle = document.createElement('div');
        this._scopeToggle.className = 'files-scope-toggle';
        this._sessionBtn = document.createElement('button');
        this._sessionBtn.className = 'files-scope-btn active';
        this._sessionBtn.textContent = 'This session';
        this._sessionBtn.title = 'Files this conversation has tracked (works offline)';
        this._sessionBtn.addEventListener('click', () => this._setScope('session'));
        this._workspaceBtn = document.createElement('button');
        this._workspaceBtn.className = 'files-scope-btn';
        this._workspaceBtn.textContent = 'All files';
        this._workspaceBtn.title = 'Live listing of /workspace and /data — starts the sandbox if it is not already running';
        this._workspaceBtn.addEventListener('click', () => this._setScope('workspace'));
        this._scopeToggle.appendChild(this._sessionBtn);
        this._scopeToggle.appendChild(this._workspaceBtn);
        node.appendChild(this._scopeToggle);

        this._filesList = document.createElement('div');
        this._filesList.className = 'files-list';
        node.appendChild(this._filesList);
    }

    private _setScope(scope: FileScope): void {
        if (scope === this._scope) return;
        this._scope = scope;
        this._sessionBtn.classList.toggle('active', scope === 'session');
        this._workspaceBtn.classList.toggle('active', scope === 'workspace');
        void this.loadFiles();
    }

    async loadFiles(): Promise<void> {
        const token = ++this._loadToken;
        const requestedScope = this._scope;
        const convId = this._getConversationId();
        if (!convId) {
            this._filesList.innerHTML = '<div class="files-empty">No conversation selected</div>';
            return;
        }

        // Drop this response if a newer load has started since it was
        // issued (the user toggled scope while this request was in flight).
        const isStale = (): boolean => token !== this._loadToken;

        const url = `/api/conversations/${convId}/files?scope=${requestedScope}`;
        try {
            if (requestedScope === 'workspace') {
                this._filesList.innerHTML = '<div class="files-empty">Loading workspace…</div>';
            }
            const response = await fetch(url);
            if (isStale()) return;
            if (!response.ok) {
                this._filesList.innerHTML = `<div class="files-empty">Failed to load files (${response.status})</div>`;
                return;
            }

            const files: any[] = await response.json();
            if (isStale()) return;
            // Streamed-file overlay only applies in session view (the
            // workspace view comes from the live filesystem already).
            if (requestedScope === 'session') {
                const apiPaths = new Set(files.map((f: any) => f.path));
                for (const [path, content] of Object.entries(this._streamedFiles)) {
                    if (!apiPaths.has(path)) {
                        files.push({ path, size: new Blob([content || '']).size, source: 'agent' });
                    }
                }
            }
            if (files.length === 0) {
                const empty = requestedScope === 'workspace'
                    ? 'Workspace is empty'
                    : 'No files yet';
                this._filesList.innerHTML = `<div class="files-empty">${empty}</div>`;
                return;
            }

            this._filesList.innerHTML = '';
            for (const file of files) {
                const source = file.source || 'agent';
                this._fileSources[file.path] = source;
                if (file.encoding) this._fileEncodings[file.path] = file.encoding;
                this._addFileEntry(file.path, file.size, source);
            }
        } catch (e) {
            console.error('Failed to load files:', e);
            if (isStale()) return;
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
        // Source labels match the per-volume / per-tool conventions used
        // server-side: "data" = file on the shared /data volume; "output"
        // = produced by run_file (skill); "workspace" = file on the
        // workspace volume in the live listing; "agent" = legacy / pre-
        // zero-trust state-tracked. Anything else falls through to "agent".
        const sourceText: Record<string, string> = {
            data: 'shared data',
            output: 'skill output',
            workspace: 'workspace',
            agent: 'agent-generated',
        };
        sourceLabel.textContent = sourceText[source] || sourceText.agent;
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
            this._onOpenFile(path, cached, this._fileSources[path] || 'agent', this._fileEncodings[path]);
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
            this._onOpenFile(data.path, data.content, this._fileSources[path] || data.source || 'agent', data.encoding);
        } catch (e) {
            console.error('Failed to open file:', e);
        }
    }
}
