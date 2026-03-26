import { Widget } from '@lumino/widgets';
import { parquetReadObjects } from 'hyparquet';
import { hljs, renderMarkdown } from '../lib/markdown';

const LANG_MAP: Record<string, string> = {
    'py': 'python', 'js': 'javascript', 'ts': 'typescript',
    'json': 'json', 'csv': 'plaintext', 'md': 'markdown',
    'html': 'html', 'css': 'css', 'sql': 'sql',
    'sh': 'bash', 'bash': 'bash', 'yaml': 'yaml', 'yml': 'yaml',
    'txt': 'plaintext', 'xml': 'xml', 'toml': 'toml',
};

const IMAGE_MIME: Record<string, string> = {
    'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
    'gif': 'image/gif', 'bmp': 'image/bmp', 'webp': 'image/webp',
    'svg': 'image/svg+xml', 'ico': 'image/x-icon',
};

/**
 * A tab widget that displays a single file's content with syntax highlighting.
 * Markdown files get a toggle between raw and rendered views.
 */
export class FileViewerWidget extends Widget {
    private _path: string;
    private _content: string;
    private _source: string;
    private _encoding: string | undefined;
    private _isMarkdown: boolean;
    private _isImage: boolean;
    private _isCsv: boolean;
    private _isParquet: boolean;
    private _isBinary: boolean;
    private _showRendered = true;
    private _contentContainer!: HTMLDivElement;
    private _toggleBtn: HTMLButtonElement | null = null;

    constructor(path: string, content: string, source: string = 'agent', encoding?: string) {
        super();
        this._path = path;
        this._content = content;
        this._source = source;
        this._encoding = encoding;
        this._isMarkdown = path.toLowerCase().endsWith('.md');
        const ext = path.split('.').pop()?.toLowerCase() || '';
        this._isImage = ext in IMAGE_MIME;
        this._isCsv = ext === 'csv' || ext === 'tsv';
        this._isParquet = ext === 'parquet';
        this._isBinary = encoding === 'base64' && !this._isImage && !this._isParquet;

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

        if (this._isImage) {
            const ext = this._path.split('.').pop()?.toLowerCase() || '';
            const mime = IMAGE_MIME[ext] || 'image/png';
            const wrapper = document.createElement('div');
            wrapper.style.padding = '1rem';
            wrapper.style.display = 'flex';
            wrapper.style.justifyContent = 'center';
            wrapper.style.alignItems = 'flex-start';
            const img = document.createElement('img');
            if (this._encoding === 'base64') {
                img.src = `data:${mime};base64,${this._content}`;
            } else {
                // Text content (e.g. SVG) — use a blob URL
                const blob = new Blob([this._content], { type: mime });
                img.src = URL.createObjectURL(blob);
            }
            img.style.maxWidth = '100%';
            img.style.maxHeight = '100%';
            img.alt = this._path.split('/').pop() || 'image';
            wrapper.appendChild(img);
            this._contentContainer.appendChild(wrapper);
        } else if (this._isBinary) {
            // Binary file we can't render — show placeholder with download prompt
            const wrapper = document.createElement('div');
            wrapper.style.padding = '2rem';
            wrapper.style.textAlign = 'center';
            wrapper.style.color = 'var(--text-secondary)';
            const filename = this._path.split('/').pop() || 'file';
            wrapper.innerHTML = `<p style="font-size: 1.1rem; margin-bottom: 1rem;">Binary file: ${filename}</p><p>This file type cannot be previewed. Use the Download button above to save it.</p>`;
            this._contentContainer.appendChild(wrapper);
        } else if (this._isCsv) {
            this._renderCsvTable();
        } else if (this._isParquet) {
            this._contentContainer.innerHTML = '<div style="padding: 1rem; color: var(--text-secondary);">Loading parquet data...</div>';
            this._renderParquetTable();
        } else if (this._isMarkdown && this._showRendered) {
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

    private _renderCsvTable(): void {
        const ext = this._path.split('.').pop()?.toLowerCase() || '';
        const sep = ext === 'tsv' ? '\t' : ',';
        const rows = this._parseCsv(this._content, sep);
        if (rows.length === 0) return;

        const wrapper = document.createElement('div');
        wrapper.style.padding = '0.5rem';
        wrapper.style.overflow = 'auto';

        const table = document.createElement('table');
        table.style.borderCollapse = 'collapse';
        table.style.width = '100%';
        table.style.fontSize = '0.85rem';

        // Header row
        const thead = document.createElement('thead');
        const headerRow = document.createElement('tr');
        for (const cell of rows[0]) {
            const th = document.createElement('th');
            th.textContent = cell;
            th.style.cssText = 'padding: 0.4rem 0.6rem; border: 1px solid var(--border); text-align: left; background: var(--bg-secondary); position: sticky; top: 0;';
            headerRow.appendChild(th);
        }
        thead.appendChild(headerRow);
        table.appendChild(thead);

        // Data rows (cap at 500 for performance)
        const tbody = document.createElement('tbody');
        const maxRows = Math.min(rows.length, 501);
        for (let i = 1; i < maxRows; i++) {
            const tr = document.createElement('tr');
            for (const cell of rows[i]) {
                const td = document.createElement('td');
                td.textContent = cell;
                td.style.cssText = 'padding: 0.3rem 0.6rem; border: 1px solid var(--border);';
                tr.appendChild(td);
            }
            tbody.appendChild(tr);
        }
        table.appendChild(tbody);
        wrapper.appendChild(table);

        if (rows.length > 501) {
            const note = document.createElement('p');
            note.style.cssText = 'color: var(--text-secondary); padding: 0.5rem; font-size: 0.85rem;';
            note.textContent = `Showing 500 of ${rows.length - 1} rows. Download for full data.`;
            wrapper.appendChild(note);
        }

        this._contentContainer.appendChild(wrapper);
    }

    private async _renderParquetTable(): Promise<void> {
        // Decode base64 to bytes
        const binary = atob(this._content);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);

        try {
            const rows = await parquetReadObjects({ file: bytes.buffer as ArrayBuffer, rowEnd: 500 }) as Record<string, unknown>[];
            if (!rows || rows.length === 0) {
                this._contentContainer.innerHTML = '<div style="padding: 1rem; color: var(--text-secondary);">Empty parquet file</div>';
                return;
            }

            const columns = Object.keys(rows[0]);
            const wrapper = document.createElement('div');
            wrapper.style.padding = '0.5rem';
            wrapper.style.overflow = 'auto';

            const table = document.createElement('table');
            table.style.borderCollapse = 'collapse';
            table.style.width = '100%';
            table.style.fontSize = '0.85rem';

            const thead = document.createElement('thead');
            const headerRow = document.createElement('tr');
            for (const col of columns) {
                const th = document.createElement('th');
                th.textContent = col;
                th.style.cssText = 'padding: 0.4rem 0.6rem; border: 1px solid var(--border); text-align: left; background: var(--bg-secondary); position: sticky; top: 0;';
                headerRow.appendChild(th);
            }
            thead.appendChild(headerRow);
            table.appendChild(thead);

            const tbody = document.createElement('tbody');
            for (const row of rows) {
                const tr = document.createElement('tr');
                for (const col of columns) {
                    const td = document.createElement('td');
                    const val = row[col];
                    td.textContent = val === null || val === undefined ? '' : String(val);
                    td.style.cssText = 'padding: 0.3rem 0.6rem; border: 1px solid var(--border);';
                    tr.appendChild(td);
                }
                tbody.appendChild(tr);
            }
            table.appendChild(tbody);
            wrapper.appendChild(table);

            if (rows.length === 500) {
                const note = document.createElement('p');
                note.style.cssText = 'color: var(--text-secondary); padding: 0.5rem; font-size: 0.85rem;';
                note.textContent = 'Showing first 500 rows. Download for full data.';
                wrapper.appendChild(note);
            }

            this._contentContainer.innerHTML = '';
            this._contentContainer.appendChild(wrapper);
        } catch (e) {
            this._contentContainer.innerHTML = `<div style="padding: 1rem; color: var(--text-secondary);">Failed to parse parquet file: ${e}</div>`;
        }
    }

    private _parseCsv(text: string, sep: string): string[][] {
        // Simple CSV parser handling quoted fields
        const rows: string[][] = [];
        let i = 0;
        while (i < text.length) {
            const row: string[] = [];
            while (i < text.length) {
                if (text[i] === '"') {
                    // Quoted field
                    i++;
                    let field = '';
                    while (i < text.length) {
                        if (text[i] === '"') {
                            if (i + 1 < text.length && text[i + 1] === '"') {
                                field += '"';
                                i += 2;
                            } else {
                                i++; // closing quote
                                break;
                            }
                        } else {
                            field += text[i++];
                        }
                    }
                    row.push(field);
                    if (i < text.length && text[i] === sep) i++;
                    else if (i < text.length && (text[i] === '\n' || text[i] === '\r')) {
                        if (text[i] === '\r' && i + 1 < text.length && text[i + 1] === '\n') i++;
                        i++;
                        break;
                    }
                } else {
                    // Unquoted field
                    let field = '';
                    while (i < text.length && text[i] !== sep && text[i] !== '\n' && text[i] !== '\r') {
                        field += text[i++];
                    }
                    row.push(field);
                    if (i < text.length && text[i] === sep) i++;
                    else {
                        if (text[i] === '\r' && i + 1 < text.length && text[i + 1] === '\n') i++;
                        if (i < text.length) i++;
                        break;
                    }
                }
            }
            if (row.length > 0 && !(row.length === 1 && row[0] === '')) rows.push(row);
        }
        return rows;
    }

    private _toggleView(): void {
        this._showRendered = !this._showRendered;
        if (this._toggleBtn) {
            this._toggleBtn.textContent = this._showRendered ? 'Raw' : 'Rendered';
        }
        this._renderContent();
    }

    private _download(): void {
        let blob: Blob;
        if (this._encoding === 'base64') {
            // Decode base64 to binary
            const binary = atob(this._content);
            const bytes = new Uint8Array(binary.length);
            for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
            const ext = this._path.split('.').pop()?.toLowerCase() || '';
            const mime = IMAGE_MIME[ext] || 'application/octet-stream';
            blob = new Blob([bytes], { type: mime });
        } else {
            blob = new Blob([this._content], { type: 'text/plain' });
        }
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
