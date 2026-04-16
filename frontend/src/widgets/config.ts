import { Widget, SplitPanel } from '@lumino/widgets';

function escapeHtml(str: string): string {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function escapeAttr(str: string): string {
    return str.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/**
 * Config widget — agent list + detail form + vector stores + settings.
 * Port of config.js into a Lumino widget.
 */
export class ConfigWidget extends Widget {
    private _agents: any[] = [];
    private _vectorstores: any[] = [];
    private _toolTypes: Record<string, any> = {};
    private _selectedAgentId: string | null = null;

    private _agentList!: HTMLDivElement;
    private _detail!: HTMLDivElement;
    private _vectorstoreList!: HTMLDivElement;
    private _skillList!: HTMLDivElement;
    private _skills: any[] = [];
    private _mcpList!: HTMLDivElement;
    private _mcpServers: any[] = [];
    private _credentialList!: HTMLDivElement;
    private _credentials: any[] = [];
    private _credentialsDisabled = false;

    constructor() {
        super();
        this.id = 'config';
        this.title.label = 'Config';
        this.title.closable = true;
        this.addClass('config-widget');
        this._buildDOM();
        this._loadAgents();
        this._loadSettings();
    }

    private _buildDOM(): void {
        const node = this.node;

        // Sidebar
        const sidebar = document.createElement('div');
        sidebar.className = 'config-sidebar';

        const agentsTitle = document.createElement('h3');
        agentsTitle.className = 'config-sidebar-title';
        agentsTitle.textContent = 'Agents';
        sidebar.appendChild(agentsTitle);

        this._agentList = document.createElement('div');
        this._agentList.className = 'agent-list';
        sidebar.appendChild(this._agentList);

        const addAgentBtn = document.createElement('button');
        addAgentBtn.className = 'config-btn';
        addAgentBtn.textContent = '+ Add Agent';
        addAgentBtn.addEventListener('click', () => this._showNewAgentModal());
        sidebar.appendChild(addAgentBtn);

        const resetBtn = document.createElement('button');
        resetBtn.className = 'config-btn config-btn-danger';
        resetBtn.textContent = 'Reset All to Defaults';
        resetBtn.addEventListener('click', () => this._resetAll());
        sidebar.appendChild(resetBtn);

        const kbTitle = document.createElement('h3');
        kbTitle.className = 'config-sidebar-title';
        kbTitle.style.marginTop = '1.5rem';
        kbTitle.textContent = 'Knowledge Bases';
        sidebar.appendChild(kbTitle);

        this._vectorstoreList = document.createElement('div');
        this._vectorstoreList.className = 'vectorstore-list';
        sidebar.appendChild(this._vectorstoreList);

        const addVsBtn = document.createElement('button');
        addVsBtn.className = 'config-btn';
        addVsBtn.textContent = '+ New Knowledge Base';
        addVsBtn.addEventListener('click', () => this._showNewVsModal());
        sidebar.appendChild(addVsBtn);

        const skillsTitle = document.createElement('h3');
        skillsTitle.className = 'config-sidebar-title';
        skillsTitle.style.marginTop = '1.5rem';
        skillsTitle.textContent = 'Skills';
        sidebar.appendChild(skillsTitle);

        this._skillList = document.createElement('div');
        this._skillList.className = 'vectorstore-list';
        sidebar.appendChild(this._skillList);

        const skillBtnRow = document.createElement('div');
        skillBtnRow.style.display = 'flex';
        skillBtnRow.style.gap = '0.5rem';
        const addSkillBtn = document.createElement('button');
        addSkillBtn.className = 'config-btn';
        addSkillBtn.textContent = '+ Add Skill';
        addSkillBtn.style.flex = '1';
        addSkillBtn.addEventListener('click', () => this._showNewSkillMenu());
        skillBtnRow.appendChild(addSkillBtn);
        const refreshAllBtn = document.createElement('button');
        refreshAllBtn.className = 'config-btn';
        refreshAllBtn.textContent = '⟳ Refresh All';
        refreshAllBtn.title = 'Re-pull all GitHub-installed skills from upstream';
        refreshAllBtn.addEventListener('click', () => this._refreshAllSkills(refreshAllBtn));
        skillBtnRow.appendChild(refreshAllBtn);
        sidebar.appendChild(skillBtnRow);

        const mcpTitle = document.createElement('h3');
        mcpTitle.className = 'config-sidebar-title';
        mcpTitle.style.marginTop = '1.5rem';
        mcpTitle.textContent = 'MCP Servers';
        sidebar.appendChild(mcpTitle);

        this._mcpList = document.createElement('div');
        this._mcpList.className = 'vectorstore-list';
        sidebar.appendChild(this._mcpList);

        const addMcpBtn = document.createElement('button');
        addMcpBtn.className = 'config-btn';
        addMcpBtn.textContent = '+ Add MCP Server';
        addMcpBtn.addEventListener('click', () => this._showNewMcpModal());
        sidebar.appendChild(addMcpBtn);

        const credentialsTitle = document.createElement('h3');
        credentialsTitle.className = 'config-sidebar-title';
        credentialsTitle.style.marginTop = '1.5rem';
        credentialsTitle.textContent = 'Credentials';
        sidebar.appendChild(credentialsTitle);

        this._credentialList = document.createElement('div');
        this._credentialList.className = 'vectorstore-list';
        sidebar.appendChild(this._credentialList);

        const addCredentialBtn = document.createElement('button');
        addCredentialBtn.className = 'config-btn';
        addCredentialBtn.textContent = '+ Add Credential';
        addCredentialBtn.addEventListener('click', () => this._showNewCredentialForm());
        sidebar.appendChild(addCredentialBtn);

        const settingsTitle = document.createElement('h3');
        settingsTitle.className = 'config-sidebar-title';
        settingsTitle.style.marginTop = '1.5rem';
        settingsTitle.textContent = 'Settings';
        sidebar.appendChild(settingsTitle);

        const settingsSection = document.createElement('div');
        settingsSection.className = 'settings-section';
        const loggingLabel = document.createElement('label');
        loggingLabel.className = 'setting-toggle';
        const loggingCb = document.createElement('input');
        loggingCb.type = 'checkbox';
        loggingCb.id = 'chat-logging-toggle';
        loggingCb.addEventListener('change', () => this._saveLoggingSetting(loggingCb.checked));
        loggingLabel.appendChild(loggingCb);
        loggingLabel.appendChild(document.createTextNode(' Log my chat activity'));
        settingsSection.appendChild(loggingLabel);
        sidebar.appendChild(settingsSection);

        node.appendChild(sidebar);

        // Detail panel
        this._detail = document.createElement('div');
        this._detail.className = 'config-detail';
        this._renderPlaceholder();
        node.appendChild(this._detail);
    }

    private _renderPlaceholder(): void {
        this._detail.innerHTML = '<div class="config-placeholder"><p>Select an agent to edit its configuration.</p></div>';
    }

    // --- Data Loading ---

    private async _loadToolTypes(): Promise<void> {
        const res = await fetch('/api/tool-types');
        if (!res.ok) return;
        const types = await res.json();
        this._toolTypes = {};
        for (const t of types) this._toolTypes[t.id] = t;
    }

    private async _loadVectorStores(): Promise<void> {
        const res = await fetch('/api/vectorstores');
        if (!res.ok) return;
        this._vectorstores = await res.json();
        this._renderVectorStoreList();
    }

    private async _loadAgents(): Promise<void> {
        await Promise.all([
            this._loadToolTypes(),
            this._loadVectorStores(),
            this._loadSkills(),
            this._loadMcpServers(),
            this._loadCredentials(),
        ]);
        const res = await fetch('/api/agents');
        if (!res.ok) return;
        this._agents = await res.json();
        this._renderAgentList();
        if (this._selectedAgentId) {
            const still = this._agents.find(a => a.id === this._selectedAgentId);
            if (still) this._selectAgent(this._selectedAgentId);
            else { this._selectedAgentId = null; this._renderPlaceholder(); }
        }
    }

    private async _loadSettings(): Promise<void> {
        const res = await fetch('/api/settings');
        if (!res.ok) return;
        const data = await res.json();
        const settings = data.settings || {};
        const cb = this.node.querySelector('#chat-logging-toggle') as HTMLInputElement;
        if (cb) cb.checked = settings.chat_event_logging === 'true';
    }

    private async _saveLoggingSetting(enabled: boolean): Promise<void> {
        await fetch('/api/settings/chat_event_logging', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ value: enabled ? 'true' : 'false' }),
        });
    }

    // --- Rendering ---

    private _renderAgentList(): void {
        this._agentList.innerHTML = '';
        for (const agent of this._agents) {
            const item = document.createElement('button');
            item.className = 'agent-list-item';
            if (agent.id === this._selectedAgentId) item.classList.add('active');
            if (!agent.enabled) item.classList.add('disabled');

            const name = document.createElement('span');
            name.className = 'agent-list-name';
            name.textContent = agent.name;
            item.appendChild(name);

            const type = document.createElement('span');
            type.className = 'agent-list-type';
            type.textContent = agent.type;
            item.appendChild(type);

            item.addEventListener('click', () => this._selectAgent(agent.id));
            this._agentList.appendChild(item);
        }
    }

    private _renderVectorStoreList(): void {
        this._vectorstoreList.innerHTML = '';
        for (const vs of this._vectorstores) {
            const item = document.createElement('div');
            item.className = 'vs-list-item';

            const info = document.createElement('div');
            info.className = 'vs-list-info';
            const vName = document.createElement('span');
            vName.className = 'vs-list-name';
            vName.textContent = vs.display_name;
            info.appendChild(vName);
            const count = document.createElement('span');
            count.className = 'vs-list-count';
            count.textContent = `${vs.document_count} chunks`;
            info.appendChild(count);
            item.appendChild(info);

            const actions = document.createElement('div');
            actions.className = 'vs-list-actions';
            const uploadBtn = document.createElement('button');
            uploadBtn.className = 'config-btn-sm';
            uploadBtn.textContent = 'Upload';
            uploadBtn.addEventListener('click', () => this._triggerUpload(vs.id));
            actions.appendChild(uploadBtn);
            const delBtn = document.createElement('button');
            delBtn.className = 'config-btn-sm config-btn-danger';
            delBtn.textContent = '\u00d7';
            delBtn.title = 'Delete';
            delBtn.addEventListener('click', () => this._deleteVectorStore(vs));
            actions.appendChild(delBtn);
            item.appendChild(actions);

            this._vectorstoreList.appendChild(item);
        }
    }

    private _selectAgent(agentId: string): void {
        this._selectedAgentId = agentId;
        const agent = this._agents.find(a => a.id === agentId);
        if (!agent) return this._renderPlaceholder();

        this._agentList.querySelectorAll('.agent-list-item').forEach(el => el.classList.remove('active'));
        const idx = this._agents.indexOf(agent);
        if (this._agentList.children[idx]) this._agentList.children[idx].classList.add('active');

        this._detail.innerHTML = `
            <div class="config-form">
                <div class="config-form-header">
                    <h2>${escapeHtml(agent.name)}</h2>
                    ${!agent.is_default ? '<span class="custom-badge">Custom</span>' : ''}
                </div>
                <div class="form-group">
                    <label for="edit-name">Name</label>
                    <input type="text" id="edit-name" value="${escapeAttr(agent.name)}">
                </div>
                <div class="form-group">
                    <label for="edit-model">Model</label>
                    <select id="edit-model">
                        <option value="claude-sonnet-4-20250514" ${agent.model === 'claude-sonnet-4-20250514' ? 'selected' : ''}>Claude Sonnet 4</option>
                        <option value="claude-opus-4-20250514" ${agent.model === 'claude-opus-4-20250514' ? 'selected' : ''}>Claude Opus 4</option>
                        <option value="claude-haiku-3-20240307" ${agent.model === 'claude-haiku-3-20240307' ? 'selected' : ''}>Claude Haiku 3</option>
                    </select>
                </div>
                <div class="form-group">
                    <label for="edit-prompt">System Prompt</label>
                    <textarea id="edit-prompt" rows="12">${escapeHtml(agent.system_prompt)}</textarea>
                </div>
                ${agent.type === 'supervisor' ? `
                <div class="form-group">
                    <p style="color: var(--text-secondary); font-size: 0.85rem; margin: 0;">The supervisor routes messages to worker agents — it doesn't use tools, skills, or knowledge bases directly. Assign those to worker agents instead.</p>
                </div>
                ` : ''}
                ${agent.type !== 'supervisor' ? `
                <div class="form-group">
                    <label>Tools</label>
                    <div class="tools-checkboxes">
                        ${Object.entries(this._toolTypes).map(([id, t]: [string, any]) => {
                            const checked = agent.tools.includes(id) ? 'checked' : '';
                            const hasSandbox = agent.tools.includes('sandbox:daytona');
                            const needsSandbox = t.requires_sandbox && !hasSandbox;
                            const disabled = !t.available || needsSandbox ? 'disabled' : '';
                            const cls = !t.available || needsSandbox ? 'tool-checkbox disabled' : 'tool-checkbox';
                            let badge = '';
                            if (!t.available) badge = ' <span class="coming-soon">Not configured</span>';
                            else if (needsSandbox) badge = ' <span class="coming-soon">Requires sandbox</span>';
                            const dataAttr = t.requires_sandbox ? ' data-requires-sandbox="true"' : '';
                            return `<label class="${cls}"><input type="checkbox" value="${escapeAttr(id)}" ${checked} ${disabled}${dataAttr}> ${escapeHtml(t.name)}${badge}</label>`;
                        }).join('')}
                    </div>
                </div>
                ${this._vectorstores.length > 0 ? `
                <div class="form-group">
                    <label>Knowledge Bases</label>
                    <div class="tools-checkboxes">
                        ${this._vectorstores.map(vs => {
                            const checked = (agent.vectorstore_ids || []).includes(vs.id) ? 'checked' : '';
                            return `<label class="tool-checkbox"><input type="checkbox" class="vs-checkbox" value="${escapeAttr(vs.id)}" ${checked}> ${escapeHtml(vs.display_name)} <span class="coming-soon">${vs.document_count} chunks</span></label>`;
                        }).join('')}
                    </div>
                </div>` : ''}
                ` : ''}
                <div class="config-form-actions">
                    ${agent.type !== 'supervisor' ? `<button id="delete-agent-btn" class="config-btn config-btn-danger">${agent.enabled ? 'Disable' : 'Enable'}</button>` : ''}
                    <button id="save-agent-btn" class="config-btn config-btn-primary">Save</button>
                </div>
            </div>
        `;

        this._detail.querySelector('#save-agent-btn')!.addEventListener('click', () => this._saveAgent(agent));
        const deleteBtn = this._detail.querySelector('#delete-agent-btn');
        if (deleteBtn) {
            deleteBtn.addEventListener('click', () => agent.enabled ? this._deleteAgent(agent) : this._enableAgent(agent));
        }

        // When sandbox checkbox is toggled, enable/disable skills that require it
        const sandboxCb = this._detail.querySelector('input[value="sandbox:daytona"]') as HTMLInputElement | null;
        if (sandboxCb) {
            sandboxCb.addEventListener('change', () => {
                const hasSandbox = sandboxCb.checked;
                this._detail.querySelectorAll<HTMLInputElement>('input[data-requires-sandbox="true"]').forEach(cb => {
                    const label = cb.closest('label')!;
                    if (hasSandbox) {
                        cb.disabled = false;
                        label.className = 'tool-checkbox';
                        const badge = label.querySelector('.coming-soon');
                        if (badge && badge.textContent === 'Requires sandbox') badge.remove();
                    } else {
                        cb.disabled = true;
                        cb.checked = false;
                        label.className = 'tool-checkbox disabled';
                        if (!label.querySelector('.coming-soon')) {
                            label.insertAdjacentHTML('beforeend', ' <span class="coming-soon">Requires sandbox</span>');
                        }
                    }
                });
            });
        }
    }

    // --- Agent CRUD ---

    private async _saveAgent(agent: any): Promise<void> {
        const name = (this._detail.querySelector('#edit-name') as HTMLInputElement).value.trim();
        const model = (this._detail.querySelector('#edit-model') as HTMLSelectElement).value;
        const system_prompt = (this._detail.querySelector('#edit-prompt') as HTMLTextAreaElement).value;
        const tools: string[] = [];
        this._detail.querySelectorAll<HTMLInputElement>('.tools-checkboxes input:checked:not(.vs-checkbox)').forEach(cb => tools.push(cb.value));
        const vectorstore_ids: string[] = [];
        this._detail.querySelectorAll<HTMLInputElement>('.vs-checkbox:checked').forEach(cb => vectorstore_ids.push(cb.value));

        if (!name || !system_prompt) return;

        const res = await fetch(`/api/agents/${agent.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, model, system_prompt, tools, vectorstore_ids, enabled: agent.enabled }),
        });
        if (!res.ok) return;
        this._agents = await res.json();
        this._renderAgentList();
        this._selectAgent(agent.id);
    }

    private async _deleteAgent(agent: any): Promise<void> {
        if (!confirm(`Are you sure you want to ${agent.is_default ? 'disable' : 'delete'} "${agent.name}"?`)) return;
        const res = await fetch(`/api/agents/${agent.id}`, { method: 'DELETE' });
        if (!res.ok) return;
        this._agents = await res.json();
        this._renderAgentList();
        if (agent.is_default) this._selectAgent(agent.id);
        else { this._selectedAgentId = null; this._renderPlaceholder(); }
    }

    private async _enableAgent(agent: any): Promise<void> {
        const res = await fetch(`/api/agents/${agent.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: true }),
        });
        if (!res.ok) return;
        this._agents = await res.json();
        this._renderAgentList();
        this._selectAgent(agent.id);
    }

    private async _resetAll(): Promise<void> {
        if (!confirm('Reset all agent configurations to defaults?')) return;
        const res = await fetch('/api/agents/reset', { method: 'POST' });
        if (!res.ok) return;
        this._agents = await res.json();
        this._selectedAgentId = null;
        this._renderAgentList();
        this._renderPlaceholder();
    }

    // --- Modals (simple prompt-based for now) ---

    private async _showNewAgentModal(): Promise<void> {
        const id = prompt('Agent ID (alphanumeric, starts with letter):');
        if (!id || !/^[a-zA-Z][a-zA-Z0-9_]*$/.test(id)) return;
        const name = prompt('Display Name:');
        if (!name) return;
        const system_prompt = prompt('System Prompt:');
        if (!system_prompt) return;

        const res = await fetch('/api/agents', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id, name, system_prompt, model: 'claude-sonnet-4-20250514' }),
        });
        if (!res.ok) return;
        this._agents = await res.json();
        this._renderAgentList();
        this._selectAgent(id);
    }

    private async _showNewVsModal(): Promise<void> {
        const name = prompt('Knowledge Base Name:');
        if (!name) return;
        const description = prompt('Description:') || '';

        const res = await fetch('/api/vectorstores', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, description }),
        });
        if (!res.ok) return;
        await this._loadVectorStores();
        if (this._selectedAgentId) this._selectAgent(this._selectedAgentId);
    }

    private async _deleteVectorStore(vs: any): Promise<void> {
        if (!confirm(`Delete "${vs.display_name}" and all its documents?`)) return;
        const res = await fetch(`/api/vectorstores/${vs.id}`, { method: 'DELETE' });
        if (!res.ok) return;
        await this._loadVectorStores();
        const agentsRes = await fetch('/api/agents');
        if (agentsRes.ok) this._agents = await agentsRes.json();
        this._renderAgentList();
        if (this._selectedAgentId) this._selectAgent(this._selectedAgentId);
    }

    private _triggerUpload(vsId: string): void {
        const input = document.createElement('input');
        input.type = 'file';
        input.multiple = true;
        input.accept = '.txt,.md,.pdf';
        input.addEventListener('change', () => this._uploadFiles(vsId, input.files));
        input.click();
    }

    private async _uploadFiles(vsId: string, files: FileList | null): Promise<void> {
        if (!files || files.length === 0) return;
        const formData = new FormData();
        for (const file of files) formData.append('files', file);

        await fetch(`/api/vectorstores/${vsId}/upload`, { method: 'POST', body: formData });
        await this._loadVectorStores();
        if (this._selectedAgentId) this._selectAgent(this._selectedAgentId);
    }

    // --- Skills ---

    private async _loadSkills(): Promise<void> {
        const res = await fetch('/api/skills');
        if (!res.ok) return;
        this._skills = await res.json();
        this._renderSkillList();
    }

    private _renderSkillList(): void {
        this._skillList.innerHTML = '';
        for (const skill of this._skills) {
            const item = document.createElement('div');
            item.className = 'vs-list-item';

            const info = document.createElement('div');
            info.className = 'vs-list-info';
            const name = document.createElement('span');
            name.className = 'vs-list-name';
            name.textContent = skill.name;
            info.appendChild(name);
            const meta = document.createElement('span');
            meta.className = 'vs-list-count';
            const parts = [skill.system ? 'system' : skill.source];
            if (skill.source_sha) parts.push(skill.source_sha.slice(0, 7));
            if (skill.requires_sandbox) parts.push('requires sandbox');
            meta.textContent = parts.join(' · ');
            info.appendChild(meta);
            item.appendChild(info);

            const actions = document.createElement('div');
            actions.className = 'vs-list-actions';

            // ⚠ surfaces declared-but-not-configured credentials so the user
            // resolves them BEFORE invoking the skill, not after a HITL card
            // blocks them. Click opens the config panel pre-filled with all
            // missing names so the user fills them in one form.
            const missing: string[] = Array.isArray(skill.missing_credentials)
                ? skill.missing_credentials
                : [];
            if (missing.length > 0) {
                const warnBtn = document.createElement('button');
                warnBtn.className = 'config-btn-sm config-btn-warning';
                warnBtn.textContent = '⚠';
                warnBtn.title = `Missing credential${missing.length === 1 ? '' : 's'}: ${missing.join(', ')}`;
                warnBtn.addEventListener('click', () => this.focusCredentials(missing));
                actions.appendChild(warnBtn);
            }

            const viewBtn = document.createElement('button');
            viewBtn.className = 'config-btn-sm';
            viewBtn.textContent = 'View';
            viewBtn.addEventListener('click', () => this._viewSkill(skill.id));
            actions.appendChild(viewBtn);

            if (!skill.system && skill.source === 'github') {
                const refreshBtn = document.createElement('button');
                refreshBtn.className = 'config-btn-sm';
                refreshBtn.textContent = '⟳';
                refreshBtn.title = 'Refresh from upstream';
                refreshBtn.addEventListener('click', () => this._refreshSkill(skill, refreshBtn));
                actions.appendChild(refreshBtn);
            }

            if (!skill.system) {
                const delBtn = document.createElement('button');
                delBtn.className = 'config-btn-sm config-btn-danger';
                delBtn.textContent = '\u00d7';
                delBtn.title = 'Delete';
                delBtn.addEventListener('click', () => this._deleteSkill(skill));
                actions.appendChild(delBtn);
            }

            item.appendChild(actions);
            this._skillList.appendChild(item);
        }
    }

    private async _refreshSkill(skill: any, btn: HTMLButtonElement): Promise<void> {
        const original = btn.textContent;
        btn.textContent = '…';
        btn.disabled = true;
        try {
            const res = await fetch(`/api/skills/${skill.id}/refresh`, { method: 'POST' });
            const result = await res.json();
            if (!res.ok) {
                alert(result.detail || 'Refresh failed');
                return;
            }
            if (result.status === 'updated') {
                await this._loadSkills();
            } else if (result.status === 'unchanged') {
                btn.textContent = '✓';
                setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1200);
                return;
            } else if (result.status === 'error') {
                alert(`Refresh failed: ${result.error}`);
            }
        } catch (e) {
            alert(`Refresh failed: ${e}`);
        }
        btn.textContent = original;
        btn.disabled = false;
    }

    private async _refreshAllSkills(btn: HTMLButtonElement): Promise<void> {
        const original = btn.textContent;
        btn.textContent = 'Refreshing…';
        btn.disabled = true;
        try {
            const res = await fetch('/api/skills/refresh', { method: 'POST' });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                alert(err.detail || 'Refresh failed');
                return;
            }
            const result = await res.json();
            const lines = [
                `Updated: ${result.updated.length}`,
                `Unchanged: ${result.unchanged.length}`,
            ];
            if (result.errors.length) lines.push(`Errors: ${result.errors.length}`);
            if (result.skipped.length) lines.push(`Skipped: ${result.skipped.length}`);
            alert(lines.join('\n'));
            if (result.updated.length) await this._loadSkills();
        } catch (e) {
            alert(`Refresh failed: ${e}`);
        } finally {
            btn.textContent = original;
            btn.disabled = false;
        }
    }

    private async _viewSkill(skillId: string): Promise<void> {
        const res = await fetch(`/api/skills/${skillId}`);
        if (!res.ok) return;
        const skill = await res.json();
        const sourceLabel = skill.system ? 'SYSTEM' : skill.source.toUpperCase();
        const refInfo = skill.source_ref ? ` (${escapeHtml(skill.source_ref)})` : '';

        this._detail.innerHTML = `
            <div class="config-form">
                <div class="config-form-header">
                    <h2>${escapeHtml(skill.name)}</h2>
                    <span class="custom-badge">${sourceLabel}</span>
                </div>
                <p style="color: var(--text-secondary); margin-bottom: 1rem;">${escapeHtml(skill.description)}${refInfo}</p>
                ${skill.scripts.length > 0 ? `<p style="color: var(--text-secondary); margin-bottom: 0.5rem;"><strong>Scripts:</strong> ${skill.scripts.map((s: string) => escapeHtml(s)).join(', ')}</p>` : ''}
                ${skill.references.length > 0 ? `<p style="color: var(--text-secondary); margin-bottom: 0.5rem;"><strong>References:</strong> ${skill.references.map((r: string) => escapeHtml(r)).join(', ')}</p>` : ''}
                <div class="form-group">
                    <label>SKILL.md</label>
                    <textarea rows="20" readonly style="font-family: monospace; font-size: 0.85rem;">${escapeHtml(skill.skill_md)}</textarea>
                </div>
                <div class="config-form-actions">
                    <button id="close-skill-btn" class="config-btn">Close</button>
                </div>
            </div>
        `;
        this._detail.querySelector('#close-skill-btn')!.addEventListener('click', () => this._renderPlaceholder());
    }

    private _showNewSkillMenu(): void {
        this._detail.innerHTML = `
            <div class="config-form">
                <h2>Add Skill</h2>
                <div style="display: flex; flex-direction: column; gap: 1rem; margin-top: 1rem;">
                    <button id="skill-from-github" class="config-btn config-btn-primary" style="padding: 0.75rem;">Install from GitHub</button>
                    <button id="skill-create-custom" class="config-btn config-btn-primary" style="padding: 0.75rem;">Create Custom Skill</button>
                    <button id="skill-cancel" class="config-btn">Cancel</button>
                </div>
            </div>
        `;
        this._detail.querySelector('#skill-from-github')!.addEventListener('click', () => this._showInstallSkillForm());
        this._detail.querySelector('#skill-create-custom')!.addEventListener('click', () => this._showCreateSkillForm());
        this._detail.querySelector('#skill-cancel')!.addEventListener('click', () => this._renderPlaceholder());
    }

    private _showInstallSkillForm(): void {
        this._detail.innerHTML = `
            <div class="config-form">
                <h2>Install Skills from GitHub</h2>
                <div class="form-group">
                    <label for="skill-repo">Repository (owner/repo)</label>
                    <input type="text" id="skill-repo" placeholder="rhiza-research/forecasting-skills">
                </div>
                <div class="form-group">
                    <label for="skill-path">Path (single skill, or directory of skills)</label>
                    <input type="text" id="skill-path" placeholder="skills">
                </div>
                <div class="form-group">
                    <label for="skill-ref">Branch / tag / SHA (optional)</label>
                    <input type="text" id="skill-ref" placeholder="main">
                </div>
                <div class="config-form-actions">
                    <button id="cancel-skill-btn" class="config-btn">Cancel</button>
                    <button id="search-skill-btn" class="config-btn config-btn-primary">Search</button>
                </div>
                <div id="skill-discover-results"></div>
            </div>
        `;

        this._detail.querySelector('#cancel-skill-btn')!.addEventListener('click', () => this._renderPlaceholder());
        this._detail.querySelector('#search-skill-btn')!.addEventListener('click', () => this._discoverSkills());
    }

    private async _discoverSkills(): Promise<void> {
        const repo = (this._detail.querySelector('#skill-repo') as HTMLInputElement).value.trim();
        const path = (this._detail.querySelector('#skill-path') as HTMLInputElement).value.trim();
        const ref = (this._detail.querySelector('#skill-ref') as HTMLInputElement).value.trim();
        if (!repo) return;

        const btn = this._detail.querySelector('#search-skill-btn') as HTMLButtonElement;
        const results = this._detail.querySelector('#skill-discover-results') as HTMLDivElement;
        btn.textContent = 'Searching…';
        btn.disabled = true;
        results.innerHTML = '';

        try {
            const res = await fetch('/api/skills/discover', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repo,
                    path: path || undefined,
                    ref: ref || undefined,
                }),
            });
            const data = await res.json();
            if (!res.ok) {
                results.innerHTML = `<p style="color: var(--error, #c33);">${escapeHtml(data.detail || 'Discover failed')}</p>`;
                return;
            }
            this._renderDiscoverResults(repo, ref, data, results);
        } catch (e) {
            results.innerHTML = `<p style="color: var(--error, #c33);">${escapeHtml(String(e))}</p>`;
        } finally {
            btn.textContent = 'Search';
            btn.disabled = false;
        }
    }

    private _renderDiscoverResults(
        repo: string,
        ref: string,
        data: any,
        container: HTMLDivElement,
    ): void {
        const sha = data.source_sha as string;
        const branch = data.source_branch as string;
        const available = data.available as any[];
        const skipped = data.skipped as any[];

        if (!available.length && !skipped.length) {
            container.innerHTML = '<p style="color: var(--text-secondary);">No skills found at that path.</p>';
            return;
        }

        let html = `
            <p style="color: var(--text-secondary); margin-top: 1rem;">
              Found ${available.length} skill${available.length === 1 ? '' : 's'} on
              <code>${escapeHtml(branch)}</code> at <code>${escapeHtml(sha.slice(0, 7))}</code>.
            </p>
        `;

        if (available.length) {
            // Snapshot the user's current credential names so we can flag
            // missing ones on each discovered skill. Uses the list already
            // loaded on startup; if the user added a credential in this
            // session the next _loadCredentials call would refresh it.
            const haveCreds = new Set(this._credentials.map(c => c.name));
            html += '<div style="display: flex; flex-direction: column; gap: 0.5rem; margin: 0.5rem 0;">';
            for (const skill of available) {
                const installed = skill.already_installed;
                const isInstalled = !!installed;
                const sameSha = isInstalled && installed.sha === sha;
                const tag = isInstalled
                    ? (sameSha
                        ? `<span style="color: var(--text-secondary); font-size: 0.85em;">already installed at ${escapeHtml(sha.slice(0, 7))}</span>`
                        : `<span style="color: var(--text-secondary); font-size: 0.85em;">installed at ${escapeHtml((installed.sha || '?').slice(0, 7))}, ${escapeHtml(sha.slice(0, 7))} available</span>`)
                    : '';
                // Openclaw-style env requirements. Highlight names the user
                // hasn't set yet so the warning is visible before install
                // rather than only at first run.
                const requiredEnv: string[] = Array.isArray(skill.required_env) ? skill.required_env : [];
                const missingEnv = requiredEnv.filter(n => !haveCreds.has(n));
                const envChip = requiredEnv.length
                    ? (missingEnv.length
                        ? `<span class="skill-required-env skill-required-env-missing" title="Missing credentials: ${escapeAttr(missingEnv.join(', '))}"> · ⚠ requires: ${escapeHtml(requiredEnv.join(', '))}</span>`
                        : `<span class="skill-required-env"> · requires: ${escapeHtml(requiredEnv.join(', '))}</span>`)
                    : '';
                html += `
                    <label style="display: flex; gap: 0.5rem; align-items: flex-start; cursor: pointer;">
                        <input type="checkbox"
                               class="skill-discover-check"
                               data-subpath="${escapeHtml(skill.subpath)}"
                               data-installed="${isInstalled ? '1' : '0'}"
                               ${isInstalled && sameSha ? '' : 'checked'}>
                        <span>
                            <strong>${escapeHtml(skill.name)}</strong>
                            ${skill.has_scripts ? '<span style="color: var(--text-secondary); font-size: 0.85em;"> · has scripts</span>' : ''}
                            ${envChip}
                            ${tag}
                            <br>
                            <span style="color: var(--text-secondary); font-size: 0.9em;">${escapeHtml(skill.description)}</span>
                        </span>
                    </label>
                `;
            }
            html += '</div>';
        }

        if (skipped.length) {
            html += '<details style="margin-top: 0.75rem;"><summary style="color: var(--text-secondary); font-size: 0.9em;">Skipped ' + skipped.length + '</summary><ul style="font-size: 0.85em; color: var(--text-secondary);">';
            for (const sk of skipped) {
                html += `<li><code>${escapeHtml(sk.subpath)}</code> — ${escapeHtml(sk.reason)}</li>`;
            }
            html += '</ul></details>';
        }

        if (available.length) {
            html += `
                <div class="config-form-actions" style="margin-top: 1rem;">
                    <label style="margin-right: auto; display: flex; align-items: center; gap: 0.4rem; color: var(--text-secondary); font-size: 0.9em;">
                        <input type="checkbox" id="skill-install-force"> reinstall (overwrite if already installed)
                    </label>
                    <button id="install-selected-btn" class="config-btn config-btn-primary">Install selected (<span id="install-selected-count">0</span>)</button>
                </div>
            `;
        }

        container.innerHTML = html;

        const checks = container.querySelectorAll<HTMLInputElement>('.skill-discover-check');
        const countEl = container.querySelector('#install-selected-count');
        const installBtn = container.querySelector('#install-selected-btn') as HTMLButtonElement | null;
        const updateCount = () => {
            if (!countEl) return;
            countEl.textContent = String(
                Array.from(checks).filter((c) => c.checked).length,
            );
        };
        checks.forEach((c) => c.addEventListener('change', updateCount));
        updateCount();

        if (installBtn) {
            installBtn.addEventListener('click', async () => {
                const selected = Array.from(checks)
                    .filter((c) => c.checked)
                    .map((c) => c.dataset.subpath as string);
                if (!selected.length) return;
                const force = (container.querySelector('#skill-install-force') as HTMLInputElement | null)?.checked || false;
                await this._installSelectedSkills(repo, ref, sha, selected, force, installBtn);
            });
        }
    }

    private async _installSelectedSkills(
        repo: string,
        ref: string,
        sha: string,
        paths: string[],
        force: boolean,
        btn: HTMLButtonElement,
    ): Promise<void> {
        btn.disabled = true;
        btn.textContent = 'Installing…';
        try {
            const res = await fetch('/api/skills/install', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repo,
                    paths,
                    // Pin to the same SHA the user just previewed.
                    ref: sha || ref || undefined,
                    force,
                }),
            });
            const result = await res.json();
            if (!res.ok) {
                alert(result.detail || 'Install failed');
                return;
            }
            const lines = [`Installed ${result.installed.length}`];
            if (result.skipped.length) lines.push(`Skipped ${result.skipped.length}`);
            if (result.errors.length) lines.push(`Errors ${result.errors.length}`);
            alert(lines.join('\n'));
            await this._loadSkills();
            await this._loadToolTypes();
            if (this._selectedAgentId) this._selectAgent(this._selectedAgentId);
            this._renderPlaceholder();
        } catch (e) {
            alert(`Install failed: ${e}`);
        } finally {
            btn.disabled = false;
            btn.textContent = 'Install selected';
        }
    }

    private _showCreateSkillForm(): void {
        this._detail.innerHTML = `
            <div class="config-form">
                <h2>Create Custom Skill</h2>
                <div class="form-group">
                    <label for="skill-name">Name (lowercase, hyphens)</label>
                    <input type="text" id="skill-name" placeholder="my-skill">
                </div>
                <div class="form-group">
                    <label for="skill-desc">Description</label>
                    <input type="text" id="skill-desc" placeholder="What this skill does...">
                </div>
                <div class="form-group">
                    <label for="skill-prompt">Instructions (Markdown)</label>
                    <textarea id="skill-prompt" rows="12" placeholder="# My Skill&#10;&#10;Step-by-step instructions for the agent..."></textarea>
                </div>
                <div class="config-form-actions">
                    <button id="cancel-skill-btn" class="config-btn">Cancel</button>
                    <button id="save-skill-btn" class="config-btn config-btn-primary">Create Skill</button>
                </div>
            </div>
        `;

        this._detail.querySelector('#cancel-skill-btn')!.addEventListener('click', () => this._renderPlaceholder());
        this._detail.querySelector('#save-skill-btn')!.addEventListener('click', async () => {
            const name = (this._detail.querySelector('#skill-name') as HTMLInputElement).value.trim();
            const description = (this._detail.querySelector('#skill-desc') as HTMLInputElement).value.trim();
            const prompt = (this._detail.querySelector('#skill-prompt') as HTMLTextAreaElement).value;
            if (!name || !description || !prompt) return;

            const res = await fetch('/api/skills', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, description, prompt }),
            });
            if (!res.ok) {
                const err = await res.json();
                alert(err.detail || 'Create failed');
                return;
            }
            await this._loadSkills();
            await this._loadToolTypes();
            if (this._selectedAgentId) this._selectAgent(this._selectedAgentId);
            this._renderPlaceholder();
        });
    }

    private async _deleteSkill(skill: any): Promise<void> {
        if (!confirm(`Delete skill "${skill.name}"?`)) return;
        const res = await fetch(`/api/skills/${skill.id}`, { method: 'DELETE' });
        if (!res.ok) return;
        await this._loadSkills();
        await this._loadToolTypes();
        if (this._selectedAgentId) this._selectAgent(this._selectedAgentId);
    }

    // --- MCP Servers ---

    private async _loadMcpServers(): Promise<void> {
        const res = await fetch('/api/mcp-servers');
        if (!res.ok) return;
        this._mcpServers = await res.json();
        this._renderMcpList();
    }

    private _renderMcpList(): void {
        this._mcpList.innerHTML = '';
        for (const server of this._mcpServers) {
            const item = document.createElement('div');
            item.className = 'vs-list-item';

            const info = document.createElement('div');
            info.className = 'vs-list-info';
            const name = document.createElement('span');
            name.className = 'vs-list-name';
            name.textContent = server.name;
            info.appendChild(name);
            const meta = document.createElement('span');
            meta.className = 'vs-list-count';
            meta.textContent = `${server.tool_count} tools` + (server.system ? ' · system' : '');
            info.appendChild(meta);
            item.appendChild(info);

            const actions = document.createElement('div');
            actions.className = 'vs-list-actions';

            const testBtn = document.createElement('button');
            testBtn.className = 'config-btn-sm';
            testBtn.textContent = 'Test';
            testBtn.addEventListener('click', () => this._testMcpServer(server.id, testBtn));
            actions.appendChild(testBtn);

            if (!server.system) {
                const delBtn = document.createElement('button');
                delBtn.className = 'config-btn-sm config-btn-danger';
                delBtn.textContent = '\u00d7';
                delBtn.title = 'Delete';
                delBtn.addEventListener('click', () => this._deleteMcpServer(server));
                actions.appendChild(delBtn);
            }

            item.appendChild(actions);
            this._mcpList.appendChild(item);
        }
    }

    private async _testMcpServer(serverId: string, btn: HTMLButtonElement): Promise<void> {
        btn.textContent = 'Testing...';
        btn.disabled = true;
        try {
            const res = await fetch(`/api/mcp-servers/${serverId}/test`, { method: 'POST' });
            const data = await res.json();
            btn.textContent = data.connected ? `${data.tool_count} tools` : 'Failed';
            setTimeout(() => { btn.textContent = 'Test'; btn.disabled = false; }, 2000);
        } catch {
            btn.textContent = 'Error';
            setTimeout(() => { btn.textContent = 'Test'; btn.disabled = false; }, 2000);
        }
    }

    private _showNewMcpModal(): void {
        this._detail.innerHTML = `
            <div class="config-form">
                <h2>Add MCP Server</h2>
                <div class="form-group">
                    <label for="mcp-name">Name</label>
                    <input type="text" id="mcp-name" placeholder="My MCP Server">
                </div>
                <div class="form-group">
                    <label for="mcp-url">URL (SSE endpoint)</label>
                    <input type="text" id="mcp-url" placeholder="http://localhost:8000/sse">
                </div>
                <div class="form-group">
                    <label for="mcp-transport">Transport</label>
                    <select id="mcp-transport">
                        <option value="sse" selected>SSE</option>
                    </select>
                </div>
                <div class="config-form-actions">
                    <button id="cancel-mcp-btn" class="config-btn">Cancel</button>
                    <button id="save-mcp-btn" class="config-btn config-btn-primary">Add Server</button>
                </div>
            </div>
        `;

        this._detail.querySelector('#cancel-mcp-btn')!.addEventListener('click', () => this._renderPlaceholder());
        this._detail.querySelector('#save-mcp-btn')!.addEventListener('click', async () => {
            const name = (this._detail.querySelector('#mcp-name') as HTMLInputElement).value.trim();
            const url = (this._detail.querySelector('#mcp-url') as HTMLInputElement).value.trim();
            const transport = (this._detail.querySelector('#mcp-transport') as HTMLSelectElement).value;
            if (!name || !url) return;

            const res = await fetch('/api/mcp-servers', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, url, transport }),
            });
            if (!res.ok) return;
            await this._loadMcpServers();
            await this._loadToolTypes();
            if (this._selectedAgentId) this._selectAgent(this._selectedAgentId);
            this._renderPlaceholder();
        });
    }

    private async _deleteMcpServer(server: any): Promise<void> {
        if (!confirm(`Delete MCP server "${server.name}"?`)) return;
        const res = await fetch(`/api/mcp-servers/${server.id}`, { method: 'DELETE' });
        if (!res.ok) return;
        await this._loadMcpServers();
        await this._loadToolTypes();
        if (this._selectedAgentId) this._selectAgent(this._selectedAgentId);
    }

    // --- Credentials ---

    private async _loadCredentials(): Promise<void> {
        const res = await fetch('/api/credentials');
        if (res.status === 503) {
            this._credentials = [];
            this._credentialsDisabled = true;
            this._renderCredentialList();
            return;
        }
        if (!res.ok) return;
        this._credentials = await res.json();
        this._credentialsDisabled = false;
        this._renderCredentialList();
    }

    private _renderCredentialList(): void {
        this._credentialList.innerHTML = '';
        if (this._credentialsDisabled) {
            const note = document.createElement('div');
            note.className = 'vs-list-info';
            note.style.color = 'var(--text-secondary)';
            note.style.fontSize = '0.8rem';
            note.style.padding = '0.25rem';
            note.textContent = 'Disabled — set CREDENTIAL_ENCRYPTION_KEY to enable';
            this._credentialList.appendChild(note);
            return;
        }
        for (const cred of this._credentials) {
            const item = document.createElement('div');
            item.className = 'vs-list-item';

            const info = document.createElement('div');
            info.className = 'vs-list-info';
            const name = document.createElement('span');
            name.className = 'vs-list-name';
            name.textContent = cred.name;
            info.appendChild(name);
            item.appendChild(info);

            const actions = document.createElement('div');
            actions.className = 'vs-list-actions';

            const editBtn = document.createElement('button');
            editBtn.className = 'config-btn-sm';
            editBtn.textContent = 'Edit';
            editBtn.addEventListener('click', () => this._showEditCredentialForm(cred));
            actions.appendChild(editBtn);

            const delBtn = document.createElement('button');
            delBtn.className = 'config-btn-sm config-btn-danger';
            delBtn.textContent = '\u00d7';
            delBtn.title = 'Delete';
            delBtn.addEventListener('click', () => this._deleteCredential(cred));
            actions.appendChild(delBtn);

            item.appendChild(actions);
            this._credentialList.appendChild(item);
        }
    }

    private _showNewCredentialForm(prefillName?: string): void {
        if (this._credentialsDisabled) {
            alert('Credentials feature is disabled. Set CREDENTIAL_ENCRYPTION_KEY to enable.');
            return;
        }
        const nameValueAttr = prefillName ? ` value="${escapeAttr(prefillName)}"` : '';
        this._detail.innerHTML = `
            <div class="config-form">
                <h2>Add Credential</h2>
                <p style="color: var(--text-secondary); font-size: 0.85rem; margin-top: -0.5rem;">
                    Stored encrypted. The value is never displayed back to you and never visible to the language model.
                </p>
                <div class="form-group">
                    <label for="cred-name">Name</label>
                    <input type="text" id="cred-name" placeholder="e.g. NASA_USERNAME"${nameValueAttr}>
                </div>
                <div class="form-group">
                    <label for="cred-value">Value</label>
                    <input type="password" id="cred-value">
                </div>
                <div class="config-form-actions">
                    <button id="cancel-cred-btn" class="config-btn">Cancel</button>
                    <button id="save-cred-btn" class="config-btn config-btn-primary">Save</button>
                </div>
            </div>
        `;
        // When pre-filled from an approval card the user only needs to type
        // the value, so land focus there directly. For a fresh Add-Credential
        // click we leave focus on the name field (the browser default).
        if (prefillName) {
            (this._detail.querySelector('#cred-value') as HTMLInputElement).focus();
        }
        this._detail.querySelector('#cancel-cred-btn')!.addEventListener('click', () => this._renderPlaceholder());
        this._detail.querySelector('#save-cred-btn')!.addEventListener('click', async () => {
            const name = (this._detail.querySelector('#cred-name') as HTMLInputElement).value.trim();
            const value = (this._detail.querySelector('#cred-value') as HTMLInputElement).value;
            if (!name || !value) return;
            const res = await fetch('/api/credentials', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, value }),
            });
            if (!res.ok) {
                const err = await res.json();
                alert(err.detail || 'Save failed');
                return;
            }
            await this._loadCredentials();
            this._renderPlaceholder();
            // Notify any open approval card that this name is now set. The
            // chat widget listens for this on ``document`` and unblocks in
            // place. Emitted after the list reload so any UI that queries
            // ``/api/credentials`` also sees the new row.
            document.dispatchEvent(new CustomEvent('credential-added', { detail: { name } }));
        });
    }

    /** Bring the credentials section into focus and open the right add-form.
     *
     *  Accepts:
     *  - nothing: opens an empty new-credential form (used by the "+ Add"
     *    button).
     *  - a string: opens the single-input form pre-filled with that name
     *    (used by the HITL approval card's per-name "Set credentials" button).
     *  - a string[]: opens a multi-input form with one row per name, all
     *    saved together (used by the skill-row ⚠ chip).
     *
     *  app.ts handles the dock-level "activate the Config tab" step via the
     *  view:config command; this method just renders the correct form. The
     *  two concerns are kept separate so ConfigWidget doesn't need to know
     *  about the DockPanel or command registry.
     */
    focusCredentials(name?: string | string[]): void {
        this._credentialList.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        if (Array.isArray(name)) {
            const cleaned = name.filter((n) => typeof n === 'string' && n);
            if (cleaned.length === 0) {
                this._showNewCredentialForm();
            } else if (cleaned.length === 1) {
                this._showNewCredentialForm(cleaned[0]);
            } else {
                this._showMissingCredentialsForm(cleaned);
            }
        } else {
            this._showNewCredentialForm(name);
        }
    }

    /** Render a multi-input form for entering several missing credentials at once.
     *
     *  One row per requested name, each with a value input. "Save all" issues
     *  parallel POSTs to ``/api/credentials`` and reports per-name results
     *  inline. Successful saves emit ``credential-added`` events so any open
     *  HITL approval card un-gates in place. Names that already exist in the
     *  store are still saved (treated as updates) — easier than fetching
     *  current state to filter, and matches the user intent of "I just want
     *  these set".
     */
    private _showMissingCredentialsForm(names: string[]): void {
        if (this._credentialsDisabled) {
            alert('Credentials feature is disabled. Set CREDENTIAL_ENCRYPTION_KEY to enable.');
            return;
        }
        const rows = names
            .map(
                (n) => `
            <div class="form-group" data-cred-name="${escapeAttr(n)}">
                <label>${escapeHtml(n)}</label>
                <input type="password" class="missing-cred-value" data-name="${escapeAttr(n)}">
                <span class="missing-cred-status" style="font-size: 0.85em; color: var(--text-secondary);"></span>
            </div>`,
            )
            .join('');
        this._detail.innerHTML = `
            <div class="config-form">
                <h2>Add ${names.length} missing credential${names.length === 1 ? '' : 's'}</h2>
                <p style="color: var(--text-secondary); font-size: 0.85rem; margin-top: -0.5rem;">
                    These names were declared by a skill but aren't in your store yet.
                    Each value is stored encrypted; the value is never displayed back to you and never visible to the language model.
                </p>
                ${rows}
                <div class="config-form-actions">
                    <button id="cancel-missing-cred-btn" class="config-btn">Cancel</button>
                    <button id="save-missing-cred-btn" class="config-btn config-btn-primary">Save all</button>
                </div>
            </div>
        `;
        // Land focus on the first value input so the user can start typing
        // immediately instead of clicking through.
        const firstValue = this._detail.querySelector('.missing-cred-value') as HTMLInputElement | null;
        if (firstValue) firstValue.focus();
        this._detail.querySelector('#cancel-missing-cred-btn')!.addEventListener('click', () => this._renderPlaceholder());
        this._detail.querySelector('#save-missing-cred-btn')!.addEventListener('click', async () => {
            const inputs = Array.from(
                this._detail.querySelectorAll<HTMLInputElement>('.missing-cred-value'),
            );
            const filled = inputs.filter((i) => i.value !== '');
            if (filled.length === 0) return;

            const saveBtn = this._detail.querySelector('#save-missing-cred-btn') as HTMLButtonElement;
            saveBtn.disabled = true;
            saveBtn.textContent = 'Saving…';

            // Per-name parallel POSTs. We accept partial success: a failed
            // entry stays on the form with an inline error, successful ones
            // disappear after the next reload. Keeps the user in the form
            // when only one entry was malformed.
            const results = await Promise.allSettled(
                filled.map(async (input) => {
                    const name = input.dataset.name as string;
                    const value = input.value;
                    const res = await fetch('/api/credentials', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ name, value }),
                    });
                    if (!res.ok) {
                        const err = await res.json().catch(() => ({}));
                        throw new Error(err.detail || `HTTP ${res.status}`);
                    }
                    return name;
                }),
            );

            const succeeded: string[] = [];
            const failed: { name: string; error: string }[] = [];
            results.forEach((r, i) => {
                const name = filled[i].dataset.name as string;
                if (r.status === 'fulfilled') {
                    succeeded.push(name);
                } else {
                    failed.push({ name, error: r.reason?.message || 'failed' });
                }
            });

            // Notify any open approval card per success — the chat widget
            // listens for this event and unblocks in place.
            for (const name of succeeded) {
                document.dispatchEvent(new CustomEvent('credential-added', { detail: { name } }));
            }

            await this._loadCredentials();
            await this._loadSkills();

            if (failed.length === 0) {
                this._renderPlaceholder();
                return;
            }

            // Re-render only the failed rows with their error messages so
            // the user can fix them without retyping the successful ones.
            saveBtn.disabled = false;
            saveBtn.textContent = `Save all (${failed.length} remaining)`;
            for (const input of inputs) {
                const name = input.dataset.name as string;
                const ok = succeeded.includes(name);
                const fail = failed.find((f) => f.name === name);
                const row = input.closest('[data-cred-name]') as HTMLElement | null;
                const status = row?.querySelector('.missing-cred-status') as HTMLSpanElement | null;
                if (ok) {
                    if (row) row.style.display = 'none';
                } else if (fail && status) {
                    status.textContent = `Error: ${fail.error}`;
                    status.style.color = 'var(--error, #c33)';
                    input.value = '';
                }
            }
        });
    }

    private _showEditCredentialForm(cred: any): void {
        this._detail.innerHTML = `
            <div class="config-form">
                <h2>Edit Credential</h2>
                <p style="color: var(--text-secondary); font-size: 0.85rem; margin-top: -0.5rem;">
                    Stored values are not displayed. Leave the value blank to keep it, or type a new one to replace it.
                </p>
                <div class="form-group">
                    <label for="cred-name">Name</label>
                    <input type="text" id="cred-name" value="${escapeAttr(cred.name)}">
                </div>
                <div class="form-group">
                    <label for="cred-value">Value</label>
                    <input type="password" id="cred-value" placeholder="(unchanged)">
                </div>
                <div class="config-form-actions">
                    <button id="cancel-cred-btn" class="config-btn">Cancel</button>
                    <button id="save-cred-btn" class="config-btn config-btn-primary">Save</button>
                </div>
            </div>
        `;
        this._detail.querySelector('#cancel-cred-btn')!.addEventListener('click', () => this._renderPlaceholder());
        this._detail.querySelector('#save-cred-btn')!.addEventListener('click', async () => {
            const name = (this._detail.querySelector('#cred-name') as HTMLInputElement).value.trim();
            const value = (this._detail.querySelector('#cred-value') as HTMLInputElement).value;
            const body: any = {};
            if (name && name !== cred.name) body.name = name;
            if (value) body.value = value;
            if (Object.keys(body).length === 0) {
                this._renderPlaceholder();
                return;
            }
            const res = await fetch(`/api/credentials/${cred.id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!res.ok) {
                const err = await res.json();
                alert(err.detail || 'Save failed');
                return;
            }
            await this._loadCredentials();
            this._renderPlaceholder();
        });
    }

    private async _deleteCredential(cred: any): Promise<void> {
        if (!confirm(`Delete credential "${cred.name}"?`)) return;
        const res = await fetch(`/api/credentials/${cred.id}`, { method: 'DELETE' });
        if (!res.ok) return;
        await this._loadCredentials();
    }
}
