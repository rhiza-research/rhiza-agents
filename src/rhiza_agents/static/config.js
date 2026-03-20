let agents = [];
let selectedAgentId = null;
let toolTypes = {}; // id -> {name, available}

const agentList = document.getElementById('agent-list');
const configDetail = document.getElementById('config-detail');
const addAgentBtn = document.getElementById('add-agent-btn');
const resetAllBtn = document.getElementById('reset-all-btn');
const modal = document.getElementById('new-agent-modal');

async function loadToolTypes() {
    const res = await fetch('/api/tool-types');
    if (!res.ok) return;
    const types = await res.json();
    toolTypes = {};
    for (const t of types) {
        toolTypes[t.id] = t;
    }
}

async function loadAgents() {
    await loadToolTypes();
    const res = await fetch('/api/agents');
    if (!res.ok) return;
    agents = await res.json();
    renderAgentList();
    if (selectedAgentId) {
        const still = agents.find(a => a.id === selectedAgentId);
        if (still) {
            selectAgent(selectedAgentId);
        } else {
            selectedAgentId = null;
            renderPlaceholder();
        }
    }
}

function renderAgentList() {
    agentList.innerHTML = '';
    for (const agent of agents) {
        const item = document.createElement('button');
        item.className = 'agent-list-item';
        if (agent.id === selectedAgentId) item.classList.add('active');
        if (!agent.enabled) item.classList.add('disabled');

        const name = document.createElement('span');
        name.className = 'agent-list-name';
        name.textContent = agent.name;
        item.appendChild(name);

        const type = document.createElement('span');
        type.className = 'agent-list-type';
        type.textContent = agent.type;
        item.appendChild(type);

        item.addEventListener('click', () => selectAgent(agent.id));
        agentList.appendChild(item);
    }
}

function renderPlaceholder() {
    configDetail.innerHTML = '<div class="config-placeholder"><p>Select an agent to edit its configuration.</p></div>';
}

function selectAgent(agentId) {
    selectedAgentId = agentId;
    const agent = agents.find(a => a.id === agentId);
    if (!agent) return renderPlaceholder();

    // Update active state in list
    agentList.querySelectorAll('.agent-list-item').forEach(el => el.classList.remove('active'));
    const idx = agents.indexOf(agent);
    if (agentList.children[idx]) agentList.children[idx].classList.add('active');

    const isSupervisor = agent.type === 'supervisor';

    configDetail.innerHTML = `
        <div class="config-form">
            <div class="config-form-header">
                <h2 id="detail-title">${escapeHtml(agent.name)}</h2>
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
                    ${Object.entries(toolTypes).map(([id, t]) => {
                        const checked = agent.tools.includes(id) ? 'checked' : '';
                        const disabled = !t.available ? 'disabled' : '';
                        const cls = !t.available ? 'tool-checkbox disabled' : 'tool-checkbox';
                        const badge = !t.available ? ' <span class="coming-soon">Not configured</span>' : '';
                        return `<label class="${cls}">
                            <input type="checkbox" value="${escapeAttr(id)}" ${checked} ${disabled}>
                            ${escapeHtml(id)}${badge}
                        </label>`;
                    }).join('')}
                </div>
            </div>

            <div class="config-form-actions">
                ${!isSupervisor ? `
                    <button id="delete-agent-btn" class="btn-danger">${agent.enabled ? 'Disable' : 'Enable'}</button>
                ` : ''}
                <button id="save-agent-btn" class="btn-primary">Save</button>
            </div>
        </div>
    `;

    document.getElementById('save-agent-btn').addEventListener('click', () => saveAgent(agent));

    const deleteBtn = document.getElementById('delete-agent-btn');
    if (deleteBtn) {
        if (agent.enabled) {
            deleteBtn.addEventListener('click', () => deleteAgent(agent));
        } else {
            deleteBtn.addEventListener('click', () => enableAgent(agent));
        }
    }
}

async function saveAgent(agent) {
    const name = document.getElementById('edit-name').value.trim();
    const model = document.getElementById('edit-model').value;
    const system_prompt = document.getElementById('edit-prompt').value;
    const tools = [];
    document.querySelectorAll('.tools-checkboxes input:checked').forEach(cb => {
        tools.push(cb.value);
    });

    if (!name) return alert('Name is required');
    if (!system_prompt) return alert('System prompt is required');

    const res = await fetch(`/api/agents/${agent.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, model, system_prompt, tools, enabled: agent.enabled }),
    });

    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        return alert(err.detail || 'Failed to save');
    }

    agents = await res.json();
    renderAgentList();
    selectAgent(agent.id);
}

async function deleteAgent(agent) {
    const action = agent.is_default ? 'disable' : 'delete';
    if (!confirm(`Are you sure you want to ${action} "${agent.name}"?`)) return;

    const res = await fetch(`/api/agents/${agent.id}`, { method: 'DELETE' });
    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        return alert(err.detail || 'Failed to delete');
    }

    agents = await res.json();
    renderAgentList();
    if (agent.is_default) {
        selectAgent(agent.id);
    } else {
        selectedAgentId = null;
        renderPlaceholder();
    }
}

async function enableAgent(agent) {
    const res = await fetch(`/api/agents/${agent.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: true }),
    });

    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        return alert(err.detail || 'Failed to enable');
    }

    agents = await res.json();
    renderAgentList();
    selectAgent(agent.id);
}

async function createAgent() {
    const id = document.getElementById('new-agent-id').value.trim();
    const name = document.getElementById('new-agent-name').value.trim();
    const system_prompt = document.getElementById('new-agent-prompt').value.trim();
    const model = document.getElementById('new-agent-model').value;

    if (!id) return alert('Agent ID is required');
    if (!/^[a-zA-Z][a-zA-Z0-9_]*$/.test(id)) return alert('Agent ID must be alphanumeric with underscores, starting with a letter');
    if (!name) return alert('Display name is required');
    if (!system_prompt) return alert('System prompt is required');

    const res = await fetch('/api/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, name, system_prompt, model }),
    });

    if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        return alert(err.detail || 'Failed to create agent');
    }

    agents = await res.json();
    modal.classList.add('hidden');
    clearNewAgentForm();
    renderAgentList();
    selectAgent(id);
}

async function resetAll() {
    if (!confirm('Reset all agent configurations to defaults? This will remove all customizations.')) return;

    const res = await fetch('/api/agents/reset', { method: 'POST' });
    if (!res.ok) return alert('Failed to reset');

    agents = await res.json();
    selectedAgentId = null;
    renderAgentList();
    renderPlaceholder();
}

function clearNewAgentForm() {
    document.getElementById('new-agent-id').value = '';
    document.getElementById('new-agent-name').value = '';
    document.getElementById('new-agent-prompt').value = '';
    document.getElementById('new-agent-model').selectedIndex = 0;
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function escapeAttr(str) {
    return str.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Event listeners
addAgentBtn.addEventListener('click', () => {
    clearNewAgentForm();
    modal.classList.remove('hidden');
});

document.getElementById('cancel-new-agent').addEventListener('click', () => {
    modal.classList.add('hidden');
});

document.getElementById('confirm-new-agent').addEventListener('click', createAgent);

modal.querySelector('.modal-backdrop').addEventListener('click', () => {
    modal.classList.add('hidden');
});

resetAllBtn.addEventListener('click', resetAll);

// Initial load
loadAgents();
