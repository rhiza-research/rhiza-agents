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

        const addSkillBtn = document.createElement('button');
        addSkillBtn.className = 'config-btn';
        addSkillBtn.textContent = '+ Add Skill';
        addSkillBtn.addEventListener('click', () => this._showNewSkillMenu());
        sidebar.appendChild(addSkillBtn);

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
        await Promise.all([this._loadToolTypes(), this._loadVectorStores(), this._loadSkills(), this._loadMcpServers()]);
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
                <div class="form-group">
                    <label>Tools</label>
                    <div class="tools-checkboxes">
                        ${Object.entries(this._toolTypes).map(([id, t]: [string, any]) => {
                            // Skills can't be assigned to the supervisor — it only delegates
                            if (agent.type === 'supervisor' && id.startsWith('skill:')) return '';
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
            const sourceLabel = skill.system ? 'system' : skill.source;
            meta.textContent = sourceLabel + (skill.requires_sandbox ? ' · requires sandbox' : '');
            info.appendChild(meta);
            item.appendChild(info);

            const actions = document.createElement('div');
            actions.className = 'vs-list-actions';

            const viewBtn = document.createElement('button');
            viewBtn.className = 'config-btn-sm';
            viewBtn.textContent = 'View';
            viewBtn.addEventListener('click', () => this._viewSkill(skill.id));
            actions.appendChild(viewBtn);

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
                <h2>Install Skill from GitHub</h2>
                <div class="form-group">
                    <label for="skill-repo">Repository (owner/repo)</label>
                    <input type="text" id="skill-repo" placeholder="anthropics/skills">
                </div>
                <div class="form-group">
                    <label for="skill-path">Path (optional subdirectory)</label>
                    <input type="text" id="skill-path" placeholder="skills/data-cleaning">
                </div>
                <div class="config-form-actions">
                    <button id="cancel-skill-btn" class="config-btn">Cancel</button>
                    <button id="install-skill-btn" class="config-btn config-btn-primary">Install</button>
                </div>
            </div>
        `;

        this._detail.querySelector('#cancel-skill-btn')!.addEventListener('click', () => this._renderPlaceholder());
        this._detail.querySelector('#install-skill-btn')!.addEventListener('click', async () => {
            const repo = (this._detail.querySelector('#skill-repo') as HTMLInputElement).value.trim();
            const path = (this._detail.querySelector('#skill-path') as HTMLInputElement).value.trim();
            if (!repo) return;

            const btn = this._detail.querySelector('#install-skill-btn') as HTMLButtonElement;
            btn.textContent = 'Installing...';
            btn.disabled = true;

            try {
                const res = await fetch('/api/skills/install', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ repo, path: path || undefined }),
                });
                if (!res.ok) {
                    const err = await res.json();
                    alert(err.detail || 'Install failed');
                    btn.textContent = 'Install';
                    btn.disabled = false;
                    return;
                }
                await this._loadSkills();
                await this._loadToolTypes();
                if (this._selectedAgentId) this._selectAgent(this._selectedAgentId);
                this._renderPlaceholder();
            } catch {
                btn.textContent = 'Error';
                setTimeout(() => { btn.textContent = 'Install'; btn.disabled = false; }, 2000);
            }
        });
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
}
