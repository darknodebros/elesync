/* ──────────────── Projects ──────────────── */
let _projectDocumentCache = new Map();
let _projectOpenRequest = 0;

async function refreshProjectsPage() {
  await loadProjects();
  const detail = document.getElementById('project-detail');
  const id = detail.dataset.projectId;
  if (id && Array.from(document.querySelectorAll('[data-project]')).some(node => node.dataset.project === id)) {
    await openProject(id);
  }
}

async function loadProjects() {
  const el = document.getElementById('projects-list');
  const data = await api('/api/projects');
  if (!data.projects.length) {
    el.innerHTML = '<div class="empty-state"><div class="empty-icon">' + icon('folders') + '</div><div class="empty-text">No projects yet</div><div class="empty-sub">Create one, then add relevant chats and memories to it.</div></div>';
    return;
  }
  el.innerHTML = data.projects.map(p => `<div class="mem-card project-list-card" data-project="${_gEsc(p.id)}" role="button" tabindex="0"><div class="mem-header"><span class="mem-type">PROJECT</span><span class="project-status status-${_gEsc(p.status || 'active')}">${_gEsc(p.status || 'active')}</span><span class="mem-date">${p.memory_count} ${p.memory_count === 1 ? 'memory' : 'memories'} · ${p.document_count || 0} ${p.document_count === 1 ? 'document' : 'documents'}</span></div><div class="mem-content">${_gEsc(p.name)}</div>${p.description ? `<div class="mem-tags">${_gEsc(p.description)}</div>` : ''}</div>`).join('');
  el.querySelectorAll('[data-project]').forEach(node => node.addEventListener('click', () => openProject(node.dataset.project)));
}

/* Suggest projects from unfiled history (imports, older saves). */
function findProjectsInHistory() {
  go('projects');
  const container = document.getElementById('project-discovery');
  if (!container) return;
  container.scrollIntoView({block:'start'});
  renderProjectDiscovery(container);
}

async function renderProjectDiscovery(container, options = {}) {
  container.innerHTML = '<section class="discovery-card"><p class="discovery-status">Looking through your history…</p></section>';
  let data;
  try {
    data = await api('/api/project-discovery');
  } catch (error) {
    container.innerHTML = `<section class="discovery-card"><p class="discovery-status">Could not look for projects: ${_gEsc(error.message)}</p></section>`;
    return;
  }
  const suggestions = data.suggestions || [];
  if (!suggestions.length) {
    container.innerHTML = options.quietWhenEmpty ? '' : `<section class="discovery-card"><p class="discovery-status"><strong>No clear projects found yet.</strong> EleSync looked at ${data.scanned || 0} items that aren't in a project. Import more history, or create a project yourself.</p><button type="button" class="text-button" data-discovery-dismiss>Close</button></section>`;
    container.querySelector('[data-discovery-dismiss]')?.addEventListener('click', () => { container.innerHTML = ''; });
    return;
  }
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  container.innerHTML = `<section class="discovery-card" aria-label="Suggested projects">
    <div class="discovery-head"><span class="dashboard-label">FROM YOUR HISTORY</span><h3>EleSync found ${plural(suggestions.length, 'possible project', 'possible projects')}</h3><p>From ${plural(data.scanned || 0, 'item', 'items')} not yet in a project. Rename or untick any, then create them.</p></div>
    <div class="discovery-list">${suggestions.map((s, index) => `
      <div class="discovery-item">
        <input type="checkbox" checked data-discovery-pick="${index}" aria-label="Create ${_gEsc(s.name)}">
        <div class="discovery-body">
          <input class="search-input discovery-name" value="${_gEsc(s.name)}" maxlength="120" data-discovery-name="${index}" aria-label="Name for suggested project ${index + 1}">
          <small>${plural(s.count, 'item', 'items')}${s.sources.length ? ' · from ' + _gEsc(s.sources.map(relayLabel).join(', ')) : ''}</small>
          ${s.samples.length ? `<ul class="discovery-samples">${s.samples.map(title => `<li>${_gEsc(title)}</li>`).join('')}</ul>` : ''}
        </div>
      </div>`).join('')}</div>
    <div class="discovery-actions"><button type="button" class="btn btn-primary" data-discovery-create></button><button type="button" class="btn" data-discovery-dismiss>Not now</button></div>
  </section>`;
  const createButton = container.querySelector('[data-discovery-create]');
  const picked = () => suggestions
    .map((s, index) => ({ s, index, on: container.querySelector(`[data-discovery-pick="${index}"]`).checked, name: container.querySelector(`[data-discovery-name="${index}"]`).value.trim() }))
    .filter(x => x.on);
  const refresh = () => {
    const n = picked().length;
    createButton.textContent = n ? `Create ${plural(n, 'project', 'projects')}` : 'Choose at least one';
    createButton.disabled = !n;
  };
  container.querySelectorAll('[data-discovery-pick]').forEach(box => box.addEventListener('change', refresh));
  refresh();
  container.querySelector('[data-discovery-dismiss]').addEventListener('click', () => { container.innerHTML = ''; });
  createButton.addEventListener('click', async () => {
    const chosen = picked();
    if (chosen.some(x => !x.name)) { toast('Every project needs a name', 'err'); return; }
    createButton.disabled = true;
    createButton.textContent = 'Creating…';
    try {
      const result = await api('/api/project-discovery/accept', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({projects: chosen.map(x => ({ name: x.name, memory_ids: x.s.memory_ids }))})});
      const created = result.created || [];
      if (created[0]) localStorage.setItem('elesync-active-project', created[0].id);
      container.innerHTML = `<section class="discovery-card is-done"><h3>Created ${plural(created.length, 'project', 'projects')}</h3><div class="discovery-created">${created.map(p => `<button type="button" class="discovery-open" data-open-project="${_gEsc(p.id)}"><strong>${_gEsc(p.name)}</strong><small>${plural(p.memory_count || 0, 'item', 'items')}</small></button>`).join('')}</div></section>`;
      container.querySelectorAll('[data-open-project]').forEach(button => button.addEventListener('click', () => { go('projects'); openProject(button.dataset.openProject); }));
      toast(`Created ${plural(created.length, 'project', 'projects')} from your history`);
      if (currentPage === 'projects') loadProjects();
      if (typeof loadImportProjects === 'function') loadImportProjects();
    } catch (error) {
      toast(error.message, 'err');
      refresh();
    }
  });
}

/* Handoff: a paste-ready prompt so any assistant can pick up the project. */
const HANDOFF_APPS = [
  ['ChatGPT', 'https://chatgpt.com/'],
  ['Claude', 'https://claude.ai/new'],
  ['Gemini', 'https://gemini.google.com/app'],
  ['Grok', 'https://grok.com/'],
];

async function openHandoff(projectId) {
  let data;
  try {
    data = await api('/api/projects/' + encodeURIComponent(projectId) + '/handoff');
  } catch (error) {
    toast(error.message, 'err');
    return;
  }
  document.getElementById('handoff-dialog')?.remove();
  const overlay = document.createElement('div');
  overlay.className = 'dialog-overlay open';
  overlay.id = 'handoff-dialog';
  overlay.innerHTML = `<div class="dialog handoff-dialog" role="dialog" aria-modal="true" aria-labelledby="handoff-title">
    <h3 id="handoff-title">Continue in another AI</h3>
    <div class="dialog-sub">Copy this and paste it as your first message in any assistant. It doesn't need to be connected to EleSync.</div>
    <textarea class="detail-input handoff-text" readonly rows="14" aria-label="Handoff prompt"></textarea>
    <div class="handoff-meta">${Number(data.characters || 0).toLocaleString()} characters</div>
    <div class="handoff-actions">
      <button type="button" class="btn btn-primary" data-handoff-copy>Copy prompt</button>
      <span class="handoff-open">Then open ${HANDOFF_APPS.map(([name, url]) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${name}</a>`).join(' · ')}</span>
      <button type="button" class="btn" data-handoff-close>Close</button>
    </div>
  </div>`;
  const text = overlay.querySelector('.handoff-text');
  text.value = data.prompt || '';
  const close = () => { overlay.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = event => { if (event.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);
  overlay.addEventListener('click', event => { if (event.target === overlay) close(); });
  overlay.querySelector('[data-handoff-close]').addEventListener('click', close);
  const copyButton = overlay.querySelector('[data-handoff-copy]');
  copyButton.addEventListener('click', async () => {
    let copied = false;
    try {
      await navigator.clipboard.writeText(text.value);
      copied = true;
    } catch (_) {
      // Clipboard API needs a secure context (plain-http LAN access); fall back.
      text.focus();
      text.select();
      copied = document.execCommand && document.execCommand('copy');
    }
    if (copied) {
      copyButton.textContent = 'Copied';
      toast('Handoff copied. Paste it into your next assistant.');
      setTimeout(() => { copyButton.textContent = 'Copy prompt'; }, 2500);
    } else {
      text.focus();
      text.select();
      toast('Select the text and copy it manually.', 'err');
    }
  });
  document.body.appendChild(overlay);
  copyButton.focus();
}

/* In-app confirmation, styled like the rest of the dashboard (the browser's
   own confirm box names providers by id and looks out of place on screen).
   Resolves true when the action is confirmed. */
function confirmDialog({ title, body, confirmLabel, danger }) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'dialog-overlay open';
    overlay.innerHTML = `<div class="dialog confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title">
      <h3 id="confirm-title">${_gEsc(title)}</h3>
      <div class="dialog-sub">${_gEsc(body || '')}</div>
      <div class="confirm-actions">
        <button type="button" class="btn" data-confirm-cancel>Cancel</button>
        <button type="button" class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-confirm-ok>${_gEsc(confirmLabel || 'Confirm')}</button>
      </div>
    </div>`;
    const finish = result => { overlay.remove(); document.removeEventListener('keydown', onKey); resolve(result); };
    const onKey = event => { if (event.key === 'Escape') finish(false); };
    document.addEventListener('keydown', onKey);
    overlay.addEventListener('click', event => { if (event.target === overlay) finish(false); });
    overlay.querySelector('[data-confirm-cancel]').addEventListener('click', () => finish(false));
    overlay.querySelector('[data-confirm-ok]').addEventListener('click', () => finish(true));
    document.body.appendChild(overlay);
    overlay.querySelector('[data-confirm-ok]').focus();
  });
}

function openProjectName() {
  return document.querySelector('#project-detail .project-summary-strip strong')?.textContent || 'this project';
}

function accessSummary(label, canRead, canWrite) {
  const project = openProjectName();
  if (canWrite) return `${label} can now read and update ${project}`;
  if (canRead) return `${label} can now read ${project}`;
  return `${label} is connected to ${project} without content access`;
}

function newProject() {
  go('projects');
  const input = document.getElementById('project-name');
  if (!input) return;
  input.scrollIntoView({block:'center'});
  input.focus();
}

function setProjectEditing(editing, project) {
  const card = document.querySelector('#project-detail > .card');
  if (!card) return;
  if (!editing && project) {
    document.getElementById('project-edit-name').value = project.name || '';
    document.getElementById('project-edit-description').value = project.description || '';
    document.getElementById('project-status-select').value = project.status || 'active';
  }
  card.classList.toggle('is-editing', editing);
  if (editing) document.getElementById('project-edit-name').focus();
  else card.querySelector('[data-project-action="edit"]')?.focus();
}

async function createProject() {
  const name = document.getElementById('project-name').value.trim();
  if (!name) { toast('Give your project a name', 'err'); return; }
  try {
    const project = await api('/api/projects', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name, description:document.getElementById('project-description').value}) });
    document.getElementById('project-name').value = '';
    document.getElementById('project-description').value = '';
    toast('Project created'); await loadProjects(); await openProject(project.id);
  } catch (e) { toast(e.message, 'err'); }
}

async function openProject(id) {
  const request = ++_projectOpenRequest;
  localStorage.setItem('elesync-active-project', id);
  const p = await api('/api/projects/' + encodeURIComponent(id));
  if (request !== _projectOpenRequest) return;
  const el = document.getElementById('project-detail');
  const panel = el.dataset.projectId === id ? (el.dataset.projectTab || 'overview') : 'overview';
  el.dataset.projectId = id;
  el.dataset.projectTab = panel;
  const projectId = p.id;
  el.innerHTML = `<div class="card"><div class="project-head"><input class="search-input project-edit-only fw-600 flex-1" id="project-edit-name" value="${_gEsc(p.name)}" aria-label="Project name"><span class="project-head-spacer project-view-only"></span><button class="btn btn-sm btn-primary project-view-only" data-project-action="handoff">Continue in another AI</button><button class="btn btn-sm project-view-only" data-project-action="edit">Edit details</button><button class="btn btn-sm project-view-only" data-project-action="export">Download brief</button><button class="btn btn-sm btn-primary project-edit-only" data-project-action="save">Save</button><button class="btn btn-sm project-edit-only" data-project-action="cancel">Cancel</button></div><div class="project-desc"><p class="project-view-only project-description-text">${p.description ? _gEsc(p.description) : '<span class="project-description-empty">No description yet. Add one so suggestions can find related items.</span>'}</p><input class="search-input project-edit-only w-full" id="project-edit-description" value="${_gEsc(p.description || '')}" placeholder="What is this project for?" aria-label="Project purpose"><div class="project-danger project-edit-only"><span>Deleting removes the project only. Every memory stays in your vault.</span><button class="btn btn-sm btn-danger" data-project-action="delete">Delete project</button></div></div><div id="project-brief"></div><div class="search-bar"><input class="search-input" id="project-ask-query" placeholder="Ask this project, for example, 'What did we decide about flights?'" aria-label="Ask this project"><button class="btn btn-primary btn-sm" data-project-action="search">Ask this project</button></div><div id="project-ask-results"></div><div class="project-source-note">Add content from any assistant: import an export, connect an AI app, or save a memory, then add it here.</div><div class="project-document-form"><div class="card-title">Project knowledge</div><div class="search-bar"><input class="search-input" id="project-document-title" placeholder="Document title" aria-label="Document title"><select class="filter-select" id="project-document-kind" aria-label="Document kind"><option value="knowledge">Knowledge</option><option value="task">Task</option><option value="decision">Decision</option><option value="handoff">AI handoff</option></select></div><textarea class="detail-input" id="project-document-body" rows="4" placeholder="Write the current context, task, decision, or handoff…"></textarea><button class="btn btn-sm" data-project-action="document">Save to project</button></div><div id="project-documents"></div><div class="project-access-panel"><div class="card-title">AI access for this project</div><div class="project-access-form"><select class="filter-select" id="project-access-provider"><option value="chatgpt">ChatGPT</option><option value="claude">Claude</option><option value="gemini">Gemini</option><option value="grok">Grok</option><option value="perplexity">Perplexity</option><option value="copilot">Copilot</option><option value="deepseek">DeepSeek</option></select><label><input type="checkbox" id="project-access-read" checked> Read</label><label><input type="checkbox" id="project-access-write"> Add updates</label><label><input type="checkbox" id="project-access-conversations"> Read conversations</label><button class="btn btn-sm" data-project-action="grant">Save access</button><button class="btn btn-primary btn-sm" data-project-action="connect">Generate project connection</button></div><div id="project-access-list"></div></div><div class="search-bar mt-12"><input class="search-input" id="project-memory-query" placeholder="Find content to add from any assistant" aria-label="Find content to add"><button class="btn btn-sm" data-project-action="find-memory">Find & add</button><button class="btn btn-primary btn-sm" data-project-action="suggest">Suggest related</button></div><div id="project-memory-results"></div><div id="project-suggestions"></div><div id="project-materials"></div></div>`;
  const projectHeader = el.querySelector('.card > div');
  projectHeader.querySelector('[data-project-action="save"]').insertAdjacentHTML('beforebegin', `<select class="filter-select project-edit-only" id="project-status-select" aria-label="Project status"><option value="active">Active</option><option value="paused">Paused</option><option value="archived">Archived</option></select>`);
  document.getElementById('project-status-select').value = p.status || 'active';
  setupProjectTabs(projectId, p);
  el.querySelector('[data-project-action="handoff"]').addEventListener('click', () => openHandoff(projectId));
  el.querySelector('[data-project-action="edit"]').addEventListener('click', () => setProjectEditing(true));
  el.querySelector('[data-project-action="cancel"]').addEventListener('click', () => setProjectEditing(false, p));
  el.querySelector('[data-project-action="save"]').addEventListener('click', () => saveProject(projectId));
  ['project-edit-name', 'project-edit-description'].forEach(fieldId => document.getElementById(fieldId).addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); saveProject(projectId); }
    else if (event.key === 'Escape') setProjectEditing(false, p);
  }));
  el.querySelector('[data-project-action="export"]').addEventListener('click', () => exportProjectBrief(projectId));
  el.querySelector('[data-project-action="delete"]').addEventListener('click', () => deleteProject(projectId));
  el.querySelector('[data-project-action="search"]').addEventListener('click', () => searchProject(projectId));
  el.querySelector('[data-project-action="find-memory"]').addEventListener('click', () => findProjectMemory(projectId));
  el.querySelector('[data-project-action="suggest"]').addEventListener('click', () => loadProjectSuggestions(projectId));
  el.querySelector('[data-project-action="document"]').addEventListener('click', () => saveProjectDocument(projectId));
  el.querySelector('[data-project-action="grant"]').addEventListener('click', () => saveProjectAccess(projectId));
  el.querySelector('[data-project-action="connect"]').addEventListener('click', () => connectProjectAI(projectId));
  renderProjectDocuments(p.documents || [], projectId);
  loadProjectAccess(projectId);
  document.getElementById('project-ask-query').addEventListener('keydown', event => {
    if (event.key === 'Enter') searchProject(projectId);
  });
  await loadProjectBrief(p.id);
  if (request !== _projectOpenRequest) return;
  renderProjectMaterials(p.memories, p.id);
}

function setupProjectTabs(projectId, project) {
  const el = document.getElementById('project-detail');
  const card = el.querySelector('.card');
  const children = Array.from(card.children);
  const groups = { overview:[0,1,2,3,4,5], knowledge:[6,7], conversations:[9,10], files:[12], access:[8], activity:[11] };
  Object.entries(groups).forEach(([panel, indexes]) => indexes.forEach(index => children[index]?.setAttribute('data-project-panel', panel)));
  children[11].innerHTML = '<div id="project-activity" class="project-activity" aria-live="polite">Loading project activity…</div>';
  el.insertAdjacentHTML('afterbegin', `<div class="project-tabs" role="tablist" aria-label="Project sections">${[['overview','Overview'],['knowledge','Knowledge'],['conversations','Conversations'],['files','Memories'],['access','AI Access'],['activity','Activity']].map(([id,label]) => `<button class="project-tab" role="tab" data-project-tab="${id}">${label}</button>`).join('')}</div><div class="project-summary-strip"><span class="project-summary-mark">${_gEsc((project.name || 'P').slice(0, 1).toUpperCase())}</span><span><strong>${_gEsc(project.name)}</strong><small>${_gEsc(project.status || 'active')} · ${project.memory_count || 0} ${project.memory_count === 1 ? 'memory' : 'memories'} · ${project.documents?.length || 0} knowledge ${project.documents?.length === 1 ? 'sheet' : 'sheets'}</small></span><button class="btn btn-sm" data-project-action="import">Import to project</button></div>`);
  el.querySelector('[data-project-action="import"]')?.addEventListener('click', () => openProjectImport(projectId));
  el.querySelectorAll('[data-project-tab]').forEach(button => button.addEventListener('click', () => selectProjectTab(button.dataset.projectTab)));
  selectProjectTab(el.dataset.projectTab || 'overview');
  loadProjectActivity(projectId);
}

function projectActivityTime(timestamp) {
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? timestamp : date.toLocaleString();
}

async function loadProjectActivity(projectId) {
  const el = document.getElementById('project-activity');
  if (!el) return;
  try {
    const data = await api('/api/projects/' + encodeURIComponent(projectId) + '/activity');
    const events = data.events || [];
    el.innerHTML = `<div class="card-title mt-14">Project activity</div>${events.length ? events.map(event => `<div class="project-activity-item activity-${_gEsc(event.type || 'recorded')}"><span class="dot ${event.type === 'access_revoked' ? 'warn' : 'on'}"></span><span><strong>${_gEsc(event.title || 'Project activity')}</strong><small>${_gEsc(event.detail || '')}${event.detail ? ' · ' : ''}${_gEsc(projectActivityTime(event.at || ''))} · ${_gEsc(event.actor || 'Unknown actor')}</small></span></div>`).join('') : '<div class="empty-sub">This project has no recorded activity yet.</div>'}`;
  } catch (error) {
    el.innerHTML = '<div class="card-title mt-14">Project activity</div><div class="empty-sub">Activity could not be loaded.</div>';
  }
}

function selectProjectTab(panel) {
  document.getElementById('project-detail').dataset.projectTab = panel;
  document.querySelectorAll('#project-detail [data-project-panel]').forEach(node => { node.style.display = node.dataset.projectPanel === panel ? '' : 'none'; });
  document.querySelectorAll('#project-detail [data-project-tab]').forEach(node => { const active = node.dataset.projectTab === panel; node.classList.toggle('active', active); node.setAttribute('aria-selected', active ? 'true' : 'false'); });
}

async function loadImportProjects() {
  const select = document.getElementById('import-project');
  if (!select) return;
  try {
    const data = await api('/api/projects');
    forgetMissingActiveProject(data.projects || []);
    const selected = select.value || _pendingImportProject || activeProjectId();
    select.innerHTML = '<option value="">Entire workspace (choose a project later)</option>' + data.projects.map(p => `<option value="${_gEsc(p.id)}">${_gEsc(p.name)} · ${_gEsc(p.status || 'active')}</option>`).join('');
    if (selected) select.value = selected;
    _pendingImportProject = '';
  } catch (_) { /* import remains available without project assignment */ }
}

function openProjectImport(projectId) {
  _pendingImportProject = projectId;
  localStorage.setItem('elesync-active-project', projectId);
  go('import');
}

function renderProjectDocuments(documents, projectId) {
  const el = document.getElementById('project-documents');
  if (!el) return;
  _projectDocumentCache = new Map(documents.map(doc => [doc.id, doc]));
  el.innerHTML = documents.length
    ? '<div class="card-title mt-14">Project records</div>' + documents.map(doc => `<div class="project-document"><span class="memory-kind">${_gEsc(doc.kind)}</span><strong>${_gEsc(doc.title)}</strong><span>${doc.kind === 'task' ? _gEsc(doc.status || 'open') + ' · ' : ''}${_gEsc(doc.updated_at || '')}</span><div class="project-document-actions"><button class="btn btn-sm" data-edit-document="${_gEsc(doc.id)}">Edit</button>${doc.kind === 'task' && doc.status !== 'completed' ? `<button class="btn btn-sm btn-primary" data-complete-task="${_gEsc(doc.id)}">Mark complete</button>` : ''}<button class="btn btn-sm btn-danger" data-delete-document="${_gEsc(doc.id)}">Delete</button></div></div>`).join('')
    : '<div class="empty-sub">No knowledge sheets yet. Save the context the next AI should receive.</div>';
  el.querySelectorAll('[data-edit-document]').forEach(button => button.addEventListener('click', () => {
    const projectDocument = _projectDocumentCache.get(button.dataset.editDocument);
    if (!projectDocument) return;
    document.getElementById('project-document-title').value = projectDocument.title;
    document.getElementById('project-document-kind').value = projectDocument.kind;
    document.getElementById('project-document-body').value = projectDocument.body;
    document.querySelector('[data-project-action="document"]').dataset.documentId = projectDocument.id;
    document.querySelector('[data-project-action="document"]').textContent = 'Save revision';
  }));
  el.querySelectorAll('[data-complete-task]').forEach(button => button.addEventListener('click', () => completeProjectTask(projectId, button.dataset.completeTask)));
  el.querySelectorAll('[data-delete-document]').forEach(button => button.addEventListener('click', () => deleteProjectDocument(projectId, button.dataset.deleteDocument)));
}

async function saveProjectDocument(projectId) {
  const title = document.getElementById('project-document-title').value.trim();
  const body = document.getElementById('project-document-body').value.trim();
  const kind = document.getElementById('project-document-kind').value;
  const saveButton = document.querySelector('[data-project-action="document"]');
  const documentId = saveButton.dataset.documentId;
  if (!title || !body) { toast('Add a title and some content', 'err'); return; }
  try {
    const url = '/api/projects/' + encodeURIComponent(projectId) + '/documents' + (documentId ? '/' + encodeURIComponent(documentId) : '');
    await api(url, {method:documentId ? 'PUT' : 'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({title, body, kind})});
    toast(documentId ? 'Project record revised' : 'Saved to project');
    openProject(projectId);
  } catch (error) { toast(error.message, 'err'); }
}

async function completeProjectTask(projectId, documentId) {
  try {
    await api('/api/projects/' + encodeURIComponent(projectId) + '/documents/' + encodeURIComponent(documentId) + '/complete', {method:'POST'});
    toast('Task completed');
    openProject(projectId);
  } catch (error) { toast(error.message, 'err'); }
}

async function deleteProjectDocument(projectId, documentId) {
  if (!await confirmDialog({ title:'Delete this project record?', body:'Its activity entry will remain.', confirmLabel:'Delete record', danger:true })) return;
  try {
    await api('/api/projects/' + encodeURIComponent(projectId) + '/documents/' + encodeURIComponent(documentId), {method:'DELETE'});
    toast('Project record deleted');
    openProject(projectId);
  } catch (error) { toast(error.message, 'err'); }
}

async function loadProjectAccess(projectId) {
  const el = document.getElementById('project-access-list');
  const data = await api('/api/projects/' + encodeURIComponent(projectId) + '/access');
  if (!data.access.length) {
    el.innerHTML = '<div class="empty-sub">No AI has access to this project yet.</div>';
    return;
  }

  el.innerHTML = '<div class="project-access-grid">' + data.access.map(item => {
    const c = connectClientMeta(item.provider);
    const active = item.status === 'active';
    const statusLabel = active ? 'Connected' : (item.status === 'revoked' ? 'Revoked' : _gEsc(item.status || 'Pending'));
    const disabled = active ? '' : ' disabled';
    return `<div class="project-access-card" style="--client-color:${c.color}">
      <div class="access-card-header">
        <strong>${_gEsc(c.label)}</strong>
        <span class="status-badge ${active ? 'active' : 'warn'}">${statusLabel}</span>
      </div>
      <div class="access-toggles">
        <label class="access-toggle"><input type="checkbox" data-permission="read" data-provider="${_gEsc(item.provider)}" aria-label="${_gEsc(c.label)} can read this project"${disabled} ${item.can_read ? 'checked' : ''}> Read project</label>
        <label class="access-toggle"><input type="checkbox" data-permission="write" data-provider="${_gEsc(item.provider)}" aria-label="${_gEsc(c.label)} can add project updates"${disabled} ${item.can_write ? 'checked' : ''}> Add updates</label>
        <label class="access-toggle"><input type="checkbox" data-permission="conversations" data-provider="${_gEsc(item.provider)}" aria-label="${_gEsc(c.label)} can read conversations"${disabled} ${item.can_read_conversations ? 'checked' : ''}> Read conversations</label>
      </div>
      <div class="access-permission-summary">${active ? (item.can_write ? 'Can read and add updates' : (item.can_read ? 'Read-only project access' : 'No project content access')) : 'Access is revoked until restored.'}</div>
      <div class="access-card-actions">
        ${active ? `<button class="btn btn-sm btn-danger" data-revoke-provider="${_gEsc(item.provider)}">Revoke access</button>` : `<button class="btn btn-sm" data-restore-provider="${_gEsc(item.provider)}">Restore access</button>`}
      </div>
    </div>`;
  }).join('') + '</div>';

  el.querySelectorAll('[data-permission]').forEach(input => input.addEventListener('change', async () => {
    const provider = input.dataset.provider;
    const card = input.closest('.project-access-card');
    const body = {
      provider: provider,
      can_read: card.querySelector('[data-permission="read"]').checked,
      can_write: card.querySelector('[data-permission="write"]').checked,
      can_read_conversations: card.querySelector('[data-permission="conversations"]').checked,
    };
    try {
      await api('/api/projects/' + encodeURIComponent(projectId) + '/access', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
      toast('Permissions updated');
    } catch (error) {
      toast(error.message, 'err');
      loadProjectAccess(projectId);
    }
  }));

  el.querySelectorAll('[data-revoke-provider]').forEach(button => button.addEventListener('click', async () => {
    const label = connectClientMeta(button.dataset.revokeProvider).label;
    const project = openProjectName();
    const confirmed = await confirmDialog({
      title: `Revoke ${label}'s access to ${project}?`,
      body: `${label} will no longer be able to read or update this project. You can restore access at any time.`,
      confirmLabel: 'Revoke access',
      danger: true,
    });
    if (!confirmed) return;
    try {
      await api('/api/projects/' + encodeURIComponent(projectId) + '/access?provider=' + encodeURIComponent(button.dataset.revokeProvider), {method:'DELETE'});
      toast(`${label} no longer has access to ${project}`);
    } catch (error) { toast(error.message, 'err'); }
    loadProjectAccess(projectId);
  }));

  el.querySelectorAll('[data-restore-provider]').forEach(button => button.addEventListener('click', async () => {
    try {
      await api('/api/projects/' + encodeURIComponent(projectId) + '/access', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({provider:button.dataset.restoreProvider, can_read:true, can_write:false, can_read_conversations:false})});
      toast(accessSummary(connectClientMeta(button.dataset.restoreProvider).label, true, false)); loadProjectAccess(projectId);
    } catch (error) { toast(error.message, 'err'); }
  }));
}

async function saveProjectAccess(projectId) {
  const body = {
    provider: document.getElementById('project-access-provider').value,
    can_read: document.getElementById('project-access-read').checked,
    can_write: document.getElementById('project-access-write').checked,
    can_read_conversations: document.getElementById('project-access-conversations').checked,
  };
  await api('/api/projects/' + encodeURIComponent(projectId) + '/access', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  toast(accessSummary(connectClientMeta(body.provider).label, body.can_read, body.can_write)); loadProjectAccess(projectId);
}

async function connectProjectAI(projectId) {
  const provider = document.getElementById('project-access-provider').value;
  if (provider !== 'claude') {
    toast('Remote project connections use OAuth after the provider is authorized.', 'err');
    return;
  }
  const body = {
    client: provider === 'claude' ? 'claude' : provider,
    project_id: projectId,
  };
  try {
    const plan = await api('/api/connect', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const target = document.getElementById('project-access-list');
    target.insertAdjacentHTML('afterbegin', `<div class="connect-output mb-10"><div class="connect-output-title">${_gEsc(plan.label)} project connection</div>${connectCopyRow('Command', plan.command)}<ol class="connect-steps">${(plan.steps || []).map(step => `<li>${_gEsc(step)}</li>`).join('')}</ol></div>`);
    bindConnectCopyButtons(target);
    toast('Project connection command ready');
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function saveProject(projectId) {
  const name = document.getElementById('project-edit-name').value.trim();
  if (!name) { toast('Project name is required', 'err'); return; }
  await api('/api/projects/' + encodeURIComponent(projectId), {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name, description:document.getElementById('project-edit-description').value, status:document.getElementById('project-status-select').value})});
  toast('Project details saved'); loadProjects(); openProject(projectId);
}

async function deleteProject(projectId) {
  if (!await confirmDialog({ title:'Delete this project?', body:'Its memories will stay in your vault.', confirmLabel:'Delete project', danger:true })) return;
  await api('/api/projects/' + encodeURIComponent(projectId), {method:'DELETE'});
  document.getElementById('project-detail').innerHTML = '';
  delete document.getElementById('project-detail').dataset.projectId;
  ++_projectOpenRequest;
  toast('Project deleted, memories kept'); loadProjects();
}

async function exportProjectBrief(projectId) {
  const data = await api('/api/projects/' + encodeURIComponent(projectId) + '/export');
  const blob = new Blob([data.markdown], {type:'text/markdown;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = data.filename; document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
  toast('Project brief downloaded');
}

async function loadProjectBrief(projectId) {
  const el = document.getElementById('project-brief');
  const brief = await api('/api/projects/' + encodeURIComponent(projectId) + '/brief');
  const labels = {key_facts:'Key facts', decisions:'Decisions', open_questions:'Open questions', next_steps:'Next steps'};
  const sectionHtml = Object.entries(labels).map(([key, label]) => {
    const rows = brief.sections[key] || [];
    if (!rows.length) return '';
    return `<div><div class="card-title mt-12">${label}</div>${rows.map(r => `<div class="mem-card" data-brief-memory="${_gEsc(r.id)}" role="button" tabindex="0"><div class="mem-header"><span class="mem-source" style="${sourceClass(r.source)}">${_gEsc(r.source)}</span><span class="mem-date">${_gEsc((r.created_at||'').slice(0,10))}</span></div><div class="mem-content">${_gEsc(String(r.content || '').slice(0, 280))}</div></div>`).join('')}</div>`;
  }).join('');
  const timelineHtml = brief.timeline.length ? `<details class="mt-12"><summary class="summary-link">Timeline (${brief.timeline.length})</summary>${brief.timeline.map(r => `<div class="mem-card" data-brief-memory="${_gEsc(r.id)}" role="button" tabindex="0"><div class="mem-header"><span class="mem-source" style="${sourceClass(r.source)}">${_gEsc(r.source)}</span><span class="mem-date">${_gEsc((r.created_at||'').slice(0,10))}</span></div><div class="mem-content">${_gEsc(String(r.content || '').slice(0, 180))}</div></div>`).join('')}</details>` : '';
  const sourceText = brief.overview.sources.length ? brief.overview.sources.join(', ') : 'none yet';
  el.innerHTML = `<div class="divided-block"><div class="card-title">Project brief</div><div class="text-secondary">${brief.overview.memory_count} ${brief.overview.memory_count === 1 ? 'memory' : 'memories'} from ${_gEsc(sourceText)}. Every point below links to its original memory.</div>${sectionHtml || '<div class="empty-sub mt-10">Add project materials to build the brief.</div>'}${timelineHtml}</div>`;
  el.querySelectorAll('[data-brief-memory]').forEach(node => node.addEventListener('click', () => openDetail(node.dataset.briefMemory)));
}

// The project's conversations are listed under Conversations; everything else
// under Memories. Each row opens the memory; Remove takes it out of the project.
function renderProjectMaterials(items, projectId) {
  const conversations = items.filter(item => item.type === 'conversation');
  const memories = items.filter(item => item.type !== 'conversation');
  const row = (item, label) => `<div class="mem-card"><div class="mem-header"><span class="mem-source" style="${sourceClass(item.source)}">${_gEsc(sourceLabel(item.source))}</span><span class="mem-type">${_gEsc(item.type)}</span><button class="btn btn-sm ml-auto" data-remove-project-memory="${_gEsc(item.id)}">Remove</button></div><button type="button" class="mem-content project-memory-open" data-open-memory="${_gEsc(item.id)}">${_gEsc(label)}</button></div>`;
  const el = document.getElementById('project-materials');
  el.innerHTML = '<div class="card-title mt-16">Project memories</div>' + (memories.length
    ? memories.map(item => row(item, String(item.content || '').slice(0, 300))).join('')
    : '<div class="empty-sub">No memories added yet.</div>');
  let list = document.getElementById('project-conversations');
  if (!list) {
    const tools = document.querySelector('#project-detail [data-project-panel="conversations"]');
    tools.insertAdjacentHTML('beforebegin', '<div id="project-conversations" data-project-panel="conversations"></div>');
    list = document.getElementById('project-conversations');
    list.style.display = tools.style.display;
  }
  list.innerHTML = '<div class="card-title">Conversations in this project</div>' + (conversations.length
    ? conversations.map(item => row(item, conversationTitle(item.content, item.source))).join('')
    : '<div class="empty-sub">No conversations yet. Find one below, or import a chat into this project.</div>');
  [el, list].forEach(container => {
    container.querySelectorAll('[data-open-memory]').forEach(button => button.addEventListener('click', () => openDetail(button.dataset.openMemory)));
    container.querySelectorAll('[data-remove-project-memory]').forEach(btn => btn.addEventListener('click', async () => { await api('/api/projects/' + encodeURIComponent(projectId) + '/memories/' + encodeURIComponent(btn.dataset.removeProjectMemory), {method:'DELETE'}); toast('Removed from project, memory kept'); openProject(projectId); loadProjects(); }));
  });
}

async function searchProject(projectId) {
  const q = document.getElementById('project-ask-query').value.trim();
  const el = document.getElementById('project-ask-results');
  if (!q) return;
  const data = await api('/api/projects/' + encodeURIComponent(projectId) + '/search?q=' + encodeURIComponent(q));
  el.innerHTML = data.items.length ? '<div class="card-title mt-14">Found in this project</div>' + data.items.map(i => `<div class="mem-card"><div class="mem-header"><span class="mem-source" style="${sourceClass(i.source)}">${_gEsc(sourceLabel(i.source))}</span></div><div class="mem-content">${_gEsc(i.content).slice(0,350)}</div></div>`).join('') : '<div class="empty-sub mt-12">Nothing in this project matches yet.</div>';
}

async function findProjectMemory(projectId) {
  const q = document.getElementById('project-memory-query').value.trim();
  if (!q) return;
  const data = await api('/api/memories?q=' + encodeURIComponent(q) + '&limit=10');
  const el = document.getElementById('project-memory-results');
  el.innerHTML = data.items.map(i => `<div class="mem-card"><div class="mem-content">${_gEsc(i.content).slice(0,180)}</div><button class="btn btn-sm" data-add-memory="${_gEsc(i.id)}">Add to project</button></div>`).join('') || '<div class="empty-sub">No matching memories.</div>';
  el.querySelectorAll('[data-add-memory]').forEach(btn => btn.addEventListener('click', async () => { await api('/api/projects/' + encodeURIComponent(projectId) + '/memories', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({memory_id:btn.dataset.addMemory})}); toast('Added to project'); openProject(projectId); loadProjects(); }));
}

async function loadProjectSuggestions(projectId) {
  const el = document.getElementById('project-suggestions');
  const data = await api('/api/projects/' + encodeURIComponent(projectId) + '/suggestions');
  if (!data.suggestions.length) { el.innerHTML = '<div class="empty-sub mt-12">No clear suggestions yet. Add a short project description or search manually.</div>'; return; }
  const mode = data.semantic_enabled ? 'Similar meaning and matching terms' : 'Matching project terms';
  el.innerHTML = `<div class="card-title mt-16">Suggested · ${mode}</div>` + data.suggestions.map(s => { const reason = s.match_kind === 'semantic' ? 'Similar meaning' : s.match_kind === 'both' ? `Similar meaning · Matched: ${s.matched_terms.map(_gEsc).join(', ')}` : `Matched: ${s.matched_terms.map(_gEsc).join(', ')}`; return `<div class="mem-card"><div class="mem-content">${_gEsc(s.memory.content).slice(0,180)}</div><div class="mem-tags">${reason}</div><button class="btn btn-sm" data-suggest-memory="${_gEsc(s.memory.id)}">Add to project</button></div>`; }).join('');
  el.querySelectorAll('[data-suggest-memory]').forEach(btn => btn.addEventListener('click', async () => { await api('/api/projects/' + encodeURIComponent(projectId) + '/memories', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({memory_id:btn.dataset.suggestMemory})}); toast('Added to project'); openProject(projectId); loadProjects(); }));
}
