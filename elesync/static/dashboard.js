/* ──────────────── State ──────────────── */
const PAGE_SIZE = 30;
let currentPage = 'overview';
let memoryView = 'all';
let detailId = null;
let conversationReader = null;
let totalMemories = 0;
let isReadOnly = false;
let currentUser = null;
let _browseTimer, _searchTimer, _importRunning = false;
let _pendingImportProject = '';
let _memSelectMode = false;
const _memSelected = new Set();
const CONNECT_CLIENTS = [
  { id:'chatgpt', label:'ChatGPT', kind:'remote', color:'var(--chatgpt)', desc:'Connect through a secure HTTPS MCP connector.' },
  { id:'claude', label:'Claude', kind:'local', color:'var(--claude)', desc:'Claude Desktop local MCP connection.' },
  { id:'gemini', label:'Gemini', kind:'gemini', color:'var(--gemini)', desc:'Connect your Gemini account with a verified API key.' },
  { id:'grok', label:'Grok', kind:'remote', color:'var(--grok)', desc:'Connect Grok to your shared EleSync project context.' },
  { id:'perplexity', label:'Perplexity', kind:'remote', color:'var(--accent2)', desc:'Connect through a secure HTTPS MCP connector.' },
  { id:'copilot', label:'Copilot', kind:'remote', color:'var(--accent)', desc:'Connect through a secure HTTPS MCP connector.' },
  { id:'deepseek', label:'DeepSeek', kind:'remote', color:'var(--deepseek)', desc:'Connect through a secure HTTPS MCP connector.' },
];

function activeProjectId() {
  return localStorage.getItem('elesync-active-project') || '';
}

// The remembered project goes stale when it is deleted or was picked while
// another vault was open. Forget it once the project list lacks it, or every
// note saved to it is refused with "project not found".
function forgetMissingActiveProject(projects) {
  const id = activeProjectId();
  if (id && !projects.some(project => project.id === id)) localStorage.removeItem('elesync-active-project');
}

function selectedImportProjectId() {
  const select = document.getElementById('import-project');
  return select ? (select.value || '') : activeProjectId();
}

function setVaultPathLabel(path) {
  const el = document.getElementById('vault-path');
  if (!el) return;
  const full = String(path || '');
  const name = full.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || full;
  el.textContent = name;
  el.title = full;
}

/* ──────────────── Init ──────────────── */
(async function init() {
  const vaultRes = await api('/api/vault');
  setVaultPathLabel(vaultRes.vault_dir);
  const meRes = await fetch('/api/me');
  const me = await meRes.json();
  currentUser = me;
  if (me.authenticated) {
    document.getElementById('auth-indicator').innerHTML =
      `<span class="text-secondary text-xs">${esc(me.username)} (${me.role})</span>` +
      ` <button class="btn btn-sm" onclick="doLogout()">Sign out</button>`;
    if (me.role === 'viewer') { isReadOnly = true; document.body.classList.add('read-only-mode'); }
  } else if (me.auth_required) {
    window.location.href = '/login';
    return;
  }
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/service-worker.js').catch(() => {});
  if (typeof setupPwaInstall === 'function') setupPwaInstall();
  showQuickstartIfNeeded();
  loadQuickstartProgress();
  // The overview contains the Companion status, so resolve its secure pairing
  // state immediately instead of waiting for the user to open another page.
  loadOverview().then(loadRemote).catch(loadRemote);
  if (typeof loadHomeGraph === 'function') loadHomeGraph();
  maybeShowSetup();
  handleShareTarget();
  flushOfflineDrafts();
  window.addEventListener('online', flushOfflineDrafts);
  setInterval(loadQuickstartProgress, 10000);
  tunnelRefresh();
  setInterval(tunnelRefresh, 15000);
  startRelay();
  setupDemoKeys();
})();

function hideQuickstart() {
  localStorage.setItem('elesync-hide-quickstart', '1');
  document.getElementById('quickstart-card')?.remove();
}
/* ──────────────── Navigation ──────────────── */
// Phones show five tabs; these destinations open from the "More" tab instead.
const MORE_PAGES = ['graph', 'connect', 'remote', 'usb'];

function toggleMobileMore(open) {
  const sheet = document.getElementById('mobile-more-sheet');
  const button = document.getElementById('nav-mobile-more');
  if (!sheet || !button) return;
  const show = open === undefined ? sheet.hidden : open;
  sheet.hidden = !show;
  button.setAttribute('aria-expanded', show ? 'true' : 'false');
}

document.addEventListener('click', event => {
  const sheet = document.getElementById('mobile-more-sheet');
  if (sheet && !sheet.hidden && !event.target.closest('#mobile-more-sheet, #nav-mobile-more')) toggleMobileMore(false);
});

// Cards that open on click but aren't <button>s carry role="button" and
// tabindex="0"; Enter or Space opens them from the keyboard as well.
document.addEventListener('keydown', event => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  const card = event.target;
  if (!(card instanceof HTMLElement) || card.tagName === 'BUTTON' || card.getAttribute('role') !== 'button') return;
  event.preventDefault();
  card.click();
});

function go(name) {
  if (typeof closeDetail === 'function') closeDetail();
  if (_memSelectMode && name !== 'browse') toggleMemorySelect(false);
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  document.getElementById('content-area').scrollTop = 0;
  window.scrollTo({top:0, left:0, behavior:'instant'});
  const nav = document.getElementById('nav-' + name);
  if (nav) {
    nav.classList.add('active');
    // If the target lives inside the collapsed "More" group, expand it so the
    // active highlight is visible rather than hidden behind the summary.
    const more = document.getElementById('nav-more');
    if (more && more.contains(nav) && !more.open) more.open = true;
  }
  currentPage = name;
  toggleMobileMore(false);
  document.getElementById('nav-mobile-more')?.classList.toggle('active', MORE_PAGES.includes(name));
  document.querySelectorAll('[data-more-page]').forEach(item => item.classList.toggle('active', item.dataset.morePage === name));
  const titles = { overview:'Home', browse:'Memories', graph:'Knowledge graph', add:'New memory',
    import:'Import', connect:'AI collaborators', projects:'Projects', conflicts:'Review conflicts', usb:'Vault & backups', remote:'Use on another device' };
  document.getElementById('topbar-title').textContent = titles[name] || name;
  document.getElementById('topbar-sub').textContent = '';
  if (name === 'overview') { loadOverview(); if (typeof loadHomeGraph === 'function') loadHomeGraph(); }
  else if (name === 'browse') loadBrowse(0);
  else if (name === 'graph') loadGraph(true);
  else if (name === 'add') loadAddProjects();
  else if (name === 'conflicts') loadConflicts();
  else if (name === 'usb') { usbRefresh(); loadBackupStatus(); }
  else if (name === 'projects') refreshProjectsPage();
  else if (name === 'import') loadImportProjects();
  else if (name === 'connect') loadConnect();
  else if (name === 'remote') {
    loadOverview().then(loadRemote).catch(loadRemote);
  }
}

/* ──────────────── Overview ──────────────── */
async function loadOverview() {
  // The stats include a whole-vault conflict scan; projects and activity come
  // back at once. Render them first so a slow count never hides the workspace.
  const statsLoaded = api('/api/stats');
  statsLoaded.catch(() => {}); // awaited below; don't report it twice if projects fail first
  const projects = await api('/api/projects');
  const auditLoaded = loadAudit();
  forgetMissingActiveProject(projects.projects || []);
  renderWorkspaceProjects(projects.projects || []);
  renderWorkspaceIdentity();
  renderActiveProject(projects.projects || []);

  const s = await statsLoaded;
  totalMemories = s.total;

  // Keep the home screen focused on the two things people need to know.
  const sources = Object.keys(s.by_source || {}).length;
  document.getElementById('stats-row').innerHTML = [
    { v: s.total, l: s.total === 1 ? 'saved memory' : 'saved memories' },
    { v: sources, l: sources === 1 ? 'AI source' : 'AI sources' },
    { v: s.conflicts || 0, l: s.conflicts === 1 ? 'thing to review' : 'things to review' },
  ].map(x => `<div class="stat"><div class="stat-value">${x.v}</div><div class="stat-label">${x.l}</div></div>`).join('');

  const byType = s.by_type || {};
  const total = s.total || 0;
  const conversations = byType.conversation || 0;
  const memories = Math.max(0, total - conversations);
  const conversationCount = document.getElementById('overview-conversations');
  if (conversationCount) conversationCount.textContent = conversations;
  const memoriesCount = document.getElementById('overview-memories');
  if (memoriesCount) memoriesCount.textContent = memories;
  const projectCount = document.getElementById('overview-projects');
  if (projectCount) projectCount.textContent = s.projects || 0;
  const notesCount = document.getElementById('overview-notes');
  if (notesCount) notesCount.textContent = byType.note || 0;
  const reviewCount = document.getElementById('overview-review');
  if (reviewCount) reviewCount.textContent = s.conflicts || 0;
  const embeddedCount = document.getElementById('overview-embedded');
  if (embeddedCount) embeddedCount.textContent = s.embedded || 0;
  const knowledgeConversations = document.getElementById('overview-knowledge-conversations');
  if (knowledgeConversations) knowledgeConversations.textContent = conversations;
  const knowledgeSources = document.getElementById('overview-knowledge-sources');
  if (knowledgeSources) knowledgeSources.textContent = sources;
  const knowledgeIndexed = document.getElementById('overview-knowledge-indexed');
  if (knowledgeIndexed) {
    knowledgeIndexed.textContent = s.embedded ? `${s.embedded}/${total}` : 'Off';
    knowledgeIndexed.parentElement.style.display = s.semantic_available ? '' : 'none';
  }
  const knowledgeNote = document.getElementById('overview-knowledge-note');
  if (knowledgeNote && total) {
    const count = (n, one, many) => `${n} ${n === 1 ? one : many}`;
    knowledgeNote.textContent = `${count(memories, 'memory', 'memories')} and ${count(conversations, 'conversation', 'conversations')} from ${count(sources, 'AI source', 'AI sources')}.`;
  }
  renderVaultHealth(s);
  renderOverviewBars('overview-sources', s.by_source || {}, 'source');
  renderOverviewBars('overview-types', byType, 'type');
  if (typeof loadPairingQr === 'function') loadPairingQr();

  const badge = document.getElementById('conflict-badge');
  if (badge) {
    badge.textContent = s.conflicts || 0;
    badge.style.display = s.conflicts > 0 ? '' : 'none';
    badge.className = 'badge warn';
  }

  // Keep source/type filters useful on the single Memories screen.
  populateFilters(s);
  await auditLoaded;
}

function renderVaultHealth(s) {
  const el = document.getElementById('overview-health');
  if (!el) return;
  const total = s.total || 0;
  const embedded = s.embedded || 0;
  const conflicts = s.conflicts || 0;
  const chips = [];

  if (!s.last_backup_at) {
    chips.push({ tone:'warn', text:'No backup yet · Back up now', tip:'Save a copy of this vault on this computer', act:'backup' });
  } else {
    const days = (Date.now() - new Date(s.last_backup_at).getTime()) / 86400000;
    const when = new Date(s.last_backup_at).toLocaleString();
    chips.push({ tone: days > 7 ? 'warn' : 'ok', text:`Backed up ${timeAgo(s.last_backup_at) || 'just now'} ago`, tip:`Last backup: ${when}. Click to back up again.`, act:'backup' });
  }

  chips.push(s.encrypted
    ? { tone:'ok', text:'Encrypted at rest' }
    : { tone:'neutral', text:'Not encrypted', tip:'Memories are plain files on this computer. Vault & backups can save an encrypted copy to a drive.', go:'usb' });

  // Only where semantic search can run: the installed app is built without it,
  // and telling it to run `ele embed` pointed at a command it doesn't have.
  if (total && s.semantic_available) {
    if (!embedded) chips.push({ tone:'neutral', text:'Semantic search off', tip:'Keyword search is always on. Add semantic recall with: ele embed' });
    else if (embedded < total) chips.push({ tone:'warn', text:`Semantic ${embedded}/${total}`, tip:'Some memories are not embedded yet. Run: ele embed' });
    else chips.push({ tone:'ok', text:'Semantic search ready' });
  }

  chips.push(conflicts
    ? { tone:'warn', text:`${conflicts} ${conflicts === 1 ? 'conflict' : 'conflicts'} to review`, go:'conflicts' }
    : { tone:'ok', text:'No conflicts' });

  el.innerHTML = chips.map(chip => {
    const inner = `<span class="health-dot ${chip.tone}" aria-hidden="true"></span>${_gEsc(chip.text)}`;
    const tip = chip.tip ? ` title="${_gEsc(chip.tip)}"` : '';
    if (chip.act) return `<button type="button" class="health-chip ${chip.tone}" data-health-act="${_gEsc(chip.act)}"${tip}>${inner}</button>`;
    return chip.go
      ? `<button type="button" class="health-chip ${chip.tone}" data-health-go="${_gEsc(chip.go)}"${tip}>${inner} →</button>`
      : `<span class="health-chip ${chip.tone}"${tip}>${inner}</span>`;
  }).join('');
  el.querySelectorAll('[data-health-go]').forEach(button => button.addEventListener('click', () => go(button.dataset.healthGo)));
  el.querySelectorAll('[data-health-act="backup"]').forEach(button => button.addEventListener('click', () => backupNow(button)));
}

// The same backup `ele backup` writes, from any "Back up now" button.
async function backupNow(button) {
  if (button) button.disabled = true;
  try {
    const result = await api('/api/backup', { method:'POST', headers:{'Content-Type':'application/json'}, body:'{}' });
    toast(result.warning || `Backed up ${result.memories} ${result.memories === 1 ? 'memory' : 'memories'}`, result.warning ? 'err' : undefined);
    if (currentPage === 'overview') loadOverview();
    else if (currentPage === 'connect') loadReadiness();
    else if (currentPage === 'usb') loadBackupStatus();
  } catch (e) {
    toast('Backup failed: ' + e.message, 'err');
  } finally {
    if (button) button.disabled = false;
  }
}

async function loadBackupStatus() {
  const el = document.getElementById('backup-status');
  if (!el) return;
  try {
    const at = ((await api('/api/readiness')).backup || {}).last_backup_at;
    el.textContent = at
      ? `Last backup ${timeAgo(at) || 'just now'} ago (${new Date(at).toLocaleString()}).`
      : 'No backup yet.';
  } catch (_) { el.textContent = ''; }
}

async function renderRecentMemories(el) {
  const empty = '<div class="empty-state compact"><div class="empty-icon">' + icon('pulse') + '</div><div class="empty-text">No AI activity yet.</div><div class="empty-sub">Saves and recalls by connected assistants appear here.</div></div>';
  try {
    const data = await api('/api/memories?limit=4&summary=1');
    const items = (data.items || []).filter(it => it && it.id);
    if (!items.length) { el.innerHTML = empty; return; }
    el.innerHTML = '<div class="recent-label">Recently saved</div>' + items.map(it => {
      const title = it.type === 'conversation' ? conversationTitle(it.content, it.source) : memoryTitle(it.content, it.source);
      const ago = timeAgo(it.created_at);
      return `<button type="button" class="recent-row" data-recent-memory="${_gEsc(it.id)}"><span class="recent-title">${_gEsc(title)}</span><span class="recent-meta">${_gEsc(sourceLabel(it.source))}${ago ? ' · ' + _gEsc(ago) + ' ago' : ''}</span></button>`;
    }).join('') + '<div class="empty-sub recent-note">Assistant saves and recalls will appear here once one is connected.</div>';
    el.querySelectorAll('[data-recent-memory]').forEach(row => row.addEventListener('click', () => openDetail(row.dataset.recentMemory)));
  } catch (_) {
    el.innerHTML = empty;
  }
}

function renderWorkspaceIdentity() {
  const name = currentUser && currentUser.username ? String(currentUser.username).trim() : '';
  const heading = document.getElementById('workspace-heading');
  const companionHeading = document.getElementById('companion-heading');
  const hour = new Date().getHours();
  const greeting = hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
  const label = name ? `${greeting}, ${name}` : greeting;
  if (heading) heading.textContent = label;
  if (companionHeading) companionHeading.textContent = label;
}

function activeWorkspaceProject(projects) {
  const preferred = activeProjectId();
  return projects.find(project => project.id === preferred) ||
    projects.find(project => project.status === 'active') || projects[0] || null;
}

function renderActiveProject(projects) {
  const project = activeWorkspaceProject(projects);
  const card = document.getElementById('overview-active-project');
  const companion = document.getElementById('companion-project');
  if (!project) {
    if (card) {
      card.classList.add('is-empty-workspace');
      card.innerHTML = '<div class="empty-workspace-copy"><h2>No projects yet</h2><p>A project groups the notes, imports and AI collaborators for one piece of work. Create one, or let EleSync suggest projects from your history.</p><div class="active-project-actions"><button class="btn btn-primary" onclick="newProject()">' + icon('plus') + 'Create project</button><button class="btn" onclick="findProjectsInHistory()">Find projects in my history</button><button class="btn" onclick="go(\'import\')">Import archive</button></div></div>';
    }
    if (companion) renderCompanionProjectPicker(companion, projects);
    return;
  }

  if (card) card.classList.remove('is-empty-workspace');

  const id = _gEsc(project.id);
  const name = _gEsc(project.name || 'Untitled project');
  const description = _gEsc(project.description || 'Shared context for this project.');
  const status = _gEsc(project.status || 'active');
  if (card) {
    card.innerHTML = `<div class="active-project-content"><div class="active-project-title"><span class="workspace-project-mark">${icon('folder-simple')}</span><div><span class="dashboard-label">ACTIVE PROJECT</span><h2>${name}</h2></div><span class="project-status status-${status}">● ${status}</span></div><p>${description}</p><p class="active-project-meta">${project.memory_count || 0} ${project.memory_count === 1 ? 'item' : 'items'} in this project</p><div class="active-project-actions"><button class="btn btn-primary" data-project-write="${id}">${icon('plus')}New memory</button><button class="btn" data-project-import="${id}">${icon('upload-simple')}Import</button><button class="btn" data-project-connect="${id}">${icon('plugs-connected')}Connect an AI</button></div></div>`;
    card.querySelector('[data-project-write]').addEventListener('click', () => { localStorage.setItem('elesync-active-project', project.id); go('add'); });
    card.querySelector('[data-project-import]').addEventListener('click', () => { localStorage.setItem('elesync-active-project', project.id); go('import'); });
    card.querySelector('[data-project-connect]').addEventListener('click', () => { localStorage.setItem('elesync-active-project', project.id); go('connect'); });
  }
  if (companion) {
    companion.innerHTML = `<button class="companion-project-card" data-companion-project="${id}"><span class="project-icon">${icon('folder-simple')}</span><span><strong>${name}</strong><small>● ${status}</small></span><b>${icon('caret-right')}</b><em>${project.memory_count || 0} items</em></button>`;
    companion.querySelector('[data-companion-project]').addEventListener('click', () => { localStorage.setItem('elesync-active-project', project.id); go('projects'); openProject(project.id); });
  }
  loadOverviewCollaborators(project.id);
}

function renderCompanionProjectPicker(container, projects) {
  if (!projects.length) {
    container.innerHTML = '<div class="companion-empty-project"><strong>Create your first project</strong><small>Projects keep notes, imports, and conversations together.</small><button type="button" class="btn btn-primary btn-sm" onclick="newProject()">Create project</button></div>';
    return;
  }
  const options = projects.map(project => `<option value="${_gEsc(project.id)}">${_gEsc(project.name || 'Untitled project')}</option>`).join('');
  container.innerHTML = `<div class="companion-project-picker"><label for="companion-project-select">Choose a project</label><select id="companion-project-select" aria-label="Choose a project for Companion"><option value="">Select a project…</option>${options}</select></div>`;
  const select = container.querySelector('#companion-project-select');
  select.addEventListener('change', () => {
    if (!select.value) return;
    localStorage.setItem('elesync-active-project', select.value);
    renderActiveProject(projects);
  });
}

async function loadOverviewCollaborators(projectId) {
  const el = document.getElementById('overview-collaborators');
  if (!el) return;
  try {
    const data = await api('/api/projects/' + encodeURIComponent(projectId) + '/access');
    const access = new Map((data.access || [])
      .filter(item => item.status !== 'revoked')
      .map(item => [String(item.provider || '').toLowerCase(), item]));
    el.innerHTML = CONNECT_CLIENTS.map(client => {
      const item = access.get(client.id);
      const connected = Boolean(item);
      const mode = connected
        ? (item.can_write ? 'Connected · can add updates' : (item.can_read ? 'Connected · read-only' : 'Connected · no access'))
        : 'Not connected · set up';
      return `<button class="collaborator-row ${connected ? 'is-connected' : 'is-available'}" data-overview-provider="${_gEsc(client.id)}">` +
        `<span class="collaborator-mark ${client.id}-mark">${_gEsc(client.label.slice(0, 1))}</span>` +
        `<span><strong>${_gEsc(client.label)}</strong><small>${_gEsc(mode)}</small></span>` +
        `<i>${icon(connected ? 'check' : 'plus')}</i></button>`;
    }).join('');
    el.querySelectorAll('[data-overview-provider]').forEach(row => row.addEventListener('click', () => go('connect')));
  } catch (error) {
    el.innerHTML = `<div class="empty-state compact"><div class="empty-text">AI access is unavailable.</div><div class="empty-sub">${_gEsc(error.message)}</div></div>`;
  }
}

function renderWorkspaceProjects(projects) {
  const el = document.getElementById('workspace-projects');
  const heading = document.getElementById('workspace-projects-heading');
  if (!el) return;
  // The active project has its own card right below (and so does the empty
  // state); list only the others, or nothing.
  const active = activeWorkspaceProject(projects);
  const others = projects.filter(p => !active || p.id !== active.id);
  if (heading) heading.style.display = others.length ? '' : 'none';
  el.style.display = others.length ? '' : 'none';
  if (!others.length) { el.innerHTML = ''; return; }
  el.innerHTML = others.slice(0, 4).map(p => `<button class="workspace-project" data-workspace-project="${_gEsc(p.id)}"><span class="workspace-project-mark">${_gEsc((p.name || 'P').slice(0, 1).toUpperCase())}</span><span class="workspace-project-copy"><strong>${_gEsc(p.name)} <span class="project-status status-${_gEsc(p.status || 'active')}">${_gEsc(p.status || 'active')}</span></strong><small>${_gEsc(p.description || 'Shared AI knowledge base')}</small></span><span class="workspace-project-count">${p.memory_count || 0}<small>items</small></span></button>`).join('');
  el.querySelectorAll('[data-workspace-project]').forEach(node => node.addEventListener('click', () => { localStorage.setItem('elesync-active-project', node.dataset.workspaceProject); go('projects'); openProject(node.dataset.workspaceProject); }));
}

function renderOverviewBars(containerId, values, kind) {
  const el = document.getElementById(containerId);
  const entries = Object.entries(values).sort((a, b) => b[1] - a[1]);
  const max = Math.max(...entries.map(entry => entry[1]), 1);
  if (!entries.length) {
    el.innerHTML = '<div class="hub-empty-stat">Nothing saved yet</div>';
    return;
  }
  el.innerHTML = entries.map(([label, count]) => {
    const readable = kind === 'type' ? memoryTypeLabel(label) : label;
    const color = kind === 'source' && label === 'grok' ? 'var(--grok)' : 'var(--accent2)';
    return `<div class="hub-bar-row"><div class="hub-bar-label">${_gEsc(readable)}</div>` +
      `<div class="hub-bar-track"><div class="hub-bar-fill" style="width:${(count / max * 100).toFixed(1)}%;background:${color}"></div></div>` +
      `<strong>${count}</strong></div>`;
  }).join('');
}

function populateFilters(stats) {
  const srcSel = document.getElementById('browse-source');
  const typeSel = document.getElementById('browse-type');
  const sources = new Set(Array.from(srcSel.options).map(option => option.value));
  Object.keys(stats.by_source || {}).sort().forEach(s => {
    if (sources.has(s)) return;
    const o = document.createElement('option'); o.value = s; o.textContent = s; srcSel.appendChild(o);
  });
  const types = new Set(Array.from(typeSel.options).map(option => option.value));
  Object.keys(stats.by_type || {}).sort().forEach(t => {
    if (types.has(t)) return;
    const o = document.createElement('option'); o.value = t; o.textContent = t; typeSel.appendChild(o);
  });
}

/* ──────────────── Browse ──────────────── */
function browseDebounce() { clearTimeout(_browseTimer); _browseTimer = setTimeout(() => loadBrowse(0), 250); }

function setMemoryView(view) {
  memoryView = view;
  document.querySelectorAll('.memory-tab').forEach(tab => {
    const active = tab.id === 'memory-tab-' + view;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  const type = document.getElementById('browse-type');
  if (type) type.value = view === 'conversation' ? 'conversation' : '';
  loadBrowse(0);
}

function memorySince() {
  const value = document.getElementById('browse-time')?.value || 'all';
  if (value === 'all') return '';
  const date = new Date();
  if (value === 'today') date.setHours(0, 0, 0, 0);
  if (value === 'week') date.setDate(date.getDate() - 7);
  if (value === 'month') date.setMonth(date.getMonth() - 1);
  return date.toISOString().slice(0, 10) + 'T00:00:00';
}

let _browseItems = [];
let _browseGen = 0;

function browseUrl(offset) {
  const q = document.getElementById('browse-q').value;
  const source = document.getElementById('browse-source').value;
  const selectedType = document.getElementById('browse-type').value;
  const type = memoryView === 'conversation' ? 'conversation' :
    (memoryView === 'memory' ? (selectedType || 'non-conversation') : selectedType);
  const since = memorySince();
  let url = `/api/memories?limit=${PAGE_SIZE}&offset=${offset}&summary=1`;
  if (q) url += `&q=${encodeURIComponent(q)}`;
  if (source) url += `&source=${encodeURIComponent(source)}`;
  if (type && type !== 'non-conversation') url += `&type=${encodeURIComponent(type)}`;
  if (type === 'non-conversation') url += '&exclude_type=conversation';
  if (since) url += `&since=${encodeURIComponent(since)}`;
  return url;
}

async function loadBrowse(offset) {
  const url = browseUrl(offset);
  const el = document.getElementById('browse-list');
  _browseGen += 1;
  await withLoader(el, [90, 72, 84], 'Memories', async function (signal) {
    const data = await api(url, { signal });
    const items = data.items || [];
    _browseItems = items;
    const summary = document.getElementById('memory-results-summary');
    if (summary) {
      const singular = data.total === 1;
      const viewLabel = memoryView === 'conversation' ? (singular ? 'conversation' : 'conversations')
        : memoryView === 'memory' ? (singular ? 'memory' : 'memories')
        : (singular ? 'knowledge item' : 'knowledge items');
      summary.textContent = `${data.total || 0} ${viewLabel}`;
    }
    if (!items.length) {
      const empty = (data.total || 0) === 0
        ? '<div class="empty-state compact"><div class="empty-icon">' + icon('brain') + '</div><div class="empty-text">Your knowledge space is empty.</div><div class="empty-sub">Import a conversation or save a note to see it organized here.</div><button class="text-button" onclick="go(\'import\')">Import something →</button></div>'
        : '<div class="empty-state compact"><div class="empty-icon">' + icon('brain') + '</div><div class="empty-text">Nothing matches these filters.</div><div class="empty-sub">Try another time period or add a new memory.</div></div>';
      el.innerHTML = empty;
    } else {
      renderMemList('browse-list', items, true);
    }
    renderLoadMore(data.total || 0);
  }, 'browse');
}

// Browsing is one growing list: 29 pages behind prev/next arrows at the very
// bottom were no way to reach older memories.
async function loadMoreBrowse(button) {
  const generation = _browseGen;
  if (button) { button.disabled = true; button.textContent = 'Loading…'; }
  try {
    const data = await api(browseUrl(_browseItems.length));
    if (generation !== _browseGen) return; // the filters changed meanwhile
    // A memory saved since the last page shifts the offsets by one.
    const seen = new Set(_browseItems.map(item => item.id));
    _browseItems = _browseItems.concat((data.items || []).filter(item => !seen.has(item.id)));
    renderMemList('browse-list', _browseItems, true);
    renderLoadMore(data.total || 0);
  } catch (e) {
    toast('Could not load more: ' + e.message, 'err');
    if (button) { button.disabled = false; button.textContent = 'Load more'; }
  }
}

function renderLoadMore(total) {
  const el = document.getElementById('browse-pagination');
  if (!el) return;
  const shown = _browseItems.length;
  if (shown >= total) {
    el.innerHTML = total > PAGE_SIZE ? `<div class="pagination"><span class="page-info">All ${total} shown</span></div>` : '';
    return;
  }
  const next = Math.min(PAGE_SIZE, total - shown);
  el.innerHTML = `<div class="pagination"><span class="page-info">Showing ${shown} of ${total}</span>` +
    `<button class="btn btn-sm" onclick="loadMoreBrowse(this)">Load ${next} more</button></div>`;
}

/* ──────────────── Search ──────────────── */
function searchDebounce() { clearTimeout(_searchTimer); _searchTimer = setTimeout(doSearch, 300); }

async function doSearch() {
  const q = document.getElementById('search-q').value.trim();
  const el = document.getElementById('search-results');
  if (!q) { el.innerHTML = '<div class="empty-state compact"><div class="empty-icon">' + icon('magnifying-glass') + '</div><div class="empty-text">Type to search your memories.</div></div>'; return; }
  await withLoader(el, [88, 70, 80], 'Search', async function (signal) {
    const data = await api('/api/memories?q=' + encodeURIComponent(q) + '&limit=50&summary=1', { signal });
    if (!data.items.length) {
      el.innerHTML = '<div class="empty-state compact"><div class="empty-icon">' + icon('magnifying-glass') + '</div><div class="empty-text">No memories match "' + _gEsc(q) + '".</div><div class="empty-sub">Try different or fewer words.</div></div>';
    } else {
      renderMemList('search-results', data.items, true);
    }
  }, 'search');
}

/* ──────────────── Memory list rendering ──────────────── */
function dayHeading(iso) {
  if (!iso) return 'Unknown date';
  const date = new Date(iso);
  const today = new Date();
  const yesterday = new Date(); yesterday.setDate(today.getDate() - 1);
  const day = date.toLocaleDateString(undefined, { year:'numeric', month:'long', day:'numeric' });
  if (date.toDateString() === today.toDateString()) return 'Today';
  if (date.toDateString() === yesterday.toDateString()) return 'Yesterday';
  return day;
}

function cardPlainText(text) {
  return String(text || '').replace(/^\s*#{1,6}\s+/gm, '')
    .replace(/^\s*(?:user|assistant|system|human|model|tool|developer):\s*/gim, '')
    .replace(/^\s*(?:[-*+] |\d+\. )/gm, '')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1').replace(/`([^`]+)`/g, '$1').trim();
}

function conversationTitle(text, source) {
  const lines = String(text || '').split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  const heading = lines.find(line => /^#{1,6}\s+/.test(line));
  return cardPlainText(heading || lines[0] || `${source || 'AI'} conversation`).slice(0, 90);
}

function memoryTitle(text, source) {
  const line = String(text || '').split(/\r?\n/).map(s => s.trim()).find(Boolean);
  return cardPlainText(line || `${source || 'Saved'} memory`).slice(0, 92);
}

function memoryPreview(text, title) {
  const lines = String(text || '').split(/\r?\n/).map(cardPlainText).filter(Boolean);
  if (lines.length && (lines[0] === title || lines[0].startsWith(title))) lines.shift();
  return lines.join(' ').replace(/\s+/g, ' ').trim().slice(0, 155);
}

function memoryTypeLabel(type) {
  return { note:'Note', fact:'Fact', preference:'Preference', instruction:'Instruction', project:'Project' }[type] || (type || 'Memory');
}

/* Display names for import sources and assistants; raw ids are lowercase. */
const SOURCE_LABELS = { manual:'Manual', chatgpt:'ChatGPT', claude:'Claude', gemini:'Gemini', grok:'Grok', perplexity:'Perplexity',
  deepseek:'DeepSeek', copilot:'Copilot', cursor:'Cursor', windsurf:'Windsurf', whatsapp:'WhatsApp', telegram:'Telegram', phone:'Phone' };
function sourceLabel(source) {
  const key = String(source || 'manual');
  return SOURCE_LABELS[key.toLowerCase()] || key.replace(/^./, char => char.toUpperCase());
}

function renderMemList(containerId, items, clickable) {
  const el = document.getElementById(containerId);
  const safeItems = (Array.isArray(items) ? items : []).filter(it => it && typeof it === 'object');
  if (!safeItems.length) {
    el.innerHTML = '<div class="empty-state"><div class="empty-icon">' + icon('tray') + '</div><div class="empty-text">No memories found</div></div>';
    return;
  }
  const groups = [];
  const byDay = new Map();
  safeItems.forEach(it => {
    const key = String(it.created_at || '').slice(0, 10) || 'unknown';
    if (!byDay.has(key)) {
      const group = { key, items: [] };
      byDay.set(key, group.items);
      groups.push(group);
    }
    byDay.get(key).push(it);
  });
  // A real button, so the card opens from the keyboard (its click bubbles to the card).
  const inspectControl = title => clickable
    ? `<button type="button" class="memory-open" aria-label="Inspect ${_gEsc(title)}">Inspect →</button>`
    : '<span class="memory-open">Inspect →</span>';
  el.innerHTML = groups.map(group => {
    const dateLabel = dayHeading(group.items[0]?.created_at || '');
    const cards = group.items.map(it => {
      const sc = sourceClass(it.source);
      const tags = (it.tags||[]).slice(0, 2).map(t => `<span class="tag">${esc(t)}</span>`).join('');
      const date = (it.created_at || '').slice(11, 16);
      const source = _gEsc(sourceLabel(it.source));
      if (it.type === 'conversation') {
        const title = conversationTitle(it.content, it.source);
        const preview = memoryPreview(it.content, title);
        return `<article class="memory-card conversation-card" id="mc-${_gEsc(it.id)}" ${clickable ? `data-mid="${_gEsc(it.id)}"` : ''}>` +
          `<div class="memory-card-top"><span class="memory-kind conversation-kind">Conversation</span><span class="memory-source-chip">${_gEsc(sourceLabel(it.source))}</span><span class="mem-date">${_gEsc(date)}</span></div>` +
          `<div class="memory-card-copy"><h3>${_gEsc(title)}</h3>${preview ? `<p>${_gEsc(preview)}</p>` : ''}</div>` +
          `<div class="memory-meta"><span class="memory-meta-source">${source}</span><span>${_gEsc(it.created_at || '').slice(0, 10)}</span>${inspectControl(title)}</div></article>`;
      }
      const title = memoryTitle(it.content, it.source);
      const preview = memoryPreview(it.content, title);
      return `<article class="memory-card note-card" id="mc-${_gEsc(it.id)}" ${clickable ? `data-mid="${_gEsc(it.id)}"` : ''}>` +
        `<div class="memory-card-top"><span class="memory-kind" style="${sc}">${_gEsc(memoryTypeLabel(it.type))}</span><span class="memory-source-chip">${_gEsc(sourceLabel(it.source))}</span><span class="mem-date">${_gEsc(date)}</span></div>` +
        `<div class="memory-card-copy"><h3>${_gEsc(title)}</h3>${preview ? `<p>${_gEsc(preview)}</p>` : ''}</div>` +
        `<div class="memory-meta"><span class="memory-meta-source">${source}</span>${tags ? `<span class="mem-tags">${tags}</span>` : ''}${inspectControl(title)}</div></article>`;
    }).join('');
    return `<section class="memory-day"><h2 class="memory-day-heading">${_gEsc(dateLabel)}<span>${group.items.length}</span></h2><div class="memory-card-grid">${cards}</div></section>`;
  }).join('');
  if (clickable) {
    const selectable = containerId === 'browse-list';
    el.querySelectorAll('.memory-card[data-mid]').forEach(function (card) {
      if (selectable && _memSelected.has(card.dataset.mid)) card.classList.add('is-selected');
      card.addEventListener('click', function () {
        if (selectable && _memSelectMode) toggleMemoryCard(card);
        else openDetail(card.dataset.mid);
      });
    });
  }
}

/* ──────────────── Bulk select (Memories) ──────────────── */
async function toggleMemorySelect(force) {
  _memSelectMode = typeof force === 'boolean' ? force : !_memSelectMode;
  _memSelected.clear();
  const toggle = document.getElementById('memory-select-toggle');
  if (toggle) {
    toggle.textContent = _memSelectMode ? 'Done' : 'Select';
    toggle.setAttribute('aria-pressed', String(_memSelectMode));
  }
  const hint = document.getElementById('memory-results-hint');
  if (hint) hint.textContent = _memSelectMode ? 'Choose items to add to a project.' : 'Select an item to inspect or edit it.';
  const list = document.getElementById('browse-list');
  if (list) {
    list.classList.toggle('is-selecting', _memSelectMode);
    list.querySelectorAll('.memory-card.is-selected').forEach(card => card.classList.remove('is-selected'));
  }
  updateBulkBar();
  if (_memSelectMode) await loadBulkProjects();
}

function toggleMemoryCard(card) {
  const id = card.dataset.mid;
  if (_memSelected.has(id)) _memSelected.delete(id); else _memSelected.add(id);
  card.classList.toggle('is-selected', _memSelected.has(id));
  updateBulkBar();
}

function updateBulkBar() {
  const bar = document.getElementById('memory-bulk-bar');
  if (!bar) return;
  bar.hidden = !_memSelectMode;
  document.getElementById('memory-bulk-count').textContent = `${_memSelected.size} selected`;
  const project = document.getElementById('memory-bulk-project');
  document.getElementById('memory-bulk-add').disabled = !_memSelected.size || !(project && project.value);
}

async function loadBulkProjects() {
  const select = document.getElementById('memory-bulk-project');
  if (!select) return;
  try {
    const data = await api('/api/projects');
    const projects = data.projects || [];
    if (!projects.length) {
      select.innerHTML = '<option value="">Create a project first</option>';
    } else {
      select.innerHTML = projects.map(p => `<option value="${_gEsc(p.id)}">${_gEsc(p.name)}</option>`).join('');
      const preferred = activeProjectId();
      if (projects.some(p => p.id === preferred)) select.value = preferred;
    }
  } catch (error) {
    select.innerHTML = '<option value="">Projects unavailable</option>';
  }
  select.onchange = updateBulkBar;
  updateBulkBar();
}

async function addSelectedToProject() {
  const select = document.getElementById('memory-bulk-project');
  const projectId = select ? select.value : '';
  if (!projectId || !_memSelected.size) return;
  const button = document.getElementById('memory-bulk-add');
  button.disabled = true;
  let added = 0;
  let already = 0;
  let failed = 0;
  for (const memoryId of Array.from(_memSelected)) {
    try {
      const result = await api('/api/projects/' + encodeURIComponent(projectId) + '/memories', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({memory_id:memoryId})});
      if (result && result.added === false) already++; else added++;
    } catch (_) { failed++; }
  }
  const name = select.options[select.selectedIndex]?.textContent || 'project';
  localStorage.setItem('elesync-active-project', projectId);
  const parts = [`Added ${added} ${added === 1 ? 'item' : 'items'} to ${name}`];
  if (already) parts.push(`${already} already there`);
  if (failed) parts.push(`${failed} failed`);
  toast(parts.join(' · '), failed ? 'err' : undefined);
  toggleMemorySelect(false);
}

function sourceClass(src) {
  const c = { chatgpt:'#10a37f', claude:'#d4a27a', gemini:'#4285f4', grok:'#b0b0c0',
    deepseek:'#22d3ee', perplexity:'#7c3aed', copilot:'#2563eb', whatsapp:'#25d366', manual:'#a78bfa' };
  const col = c[src] || 'var(--accent)';
  return `background:${col}22;color:${col};border:1px solid ${col}44`;
}

function formatConv(text) {
  if (!text) return '';
  const lines = text.split('\n');
  let html = '<div class="conv">';
  let current = '', role = '';
  for (const line of lines) {
    const m = line.match(/^(user|assistant|system):\s*(.*)/i);
    if (m) {
      if (current) html += `<div class="conv-msg ${role}">${esc(current.trim())}</div>`;
      role = m[1].toLowerCase(); current = m[2];
    } else { current += '\n' + line; }
  }
  if (current) html += `<div class="conv-msg ${role}">${esc(current.trim())}</div>`;
  return html + '</div>';
}

/* ──────────────── Detail panel ──────────────── */
function conversationMessages(content) {
  const text = String(content || '').replace(/^#[^\n]*(?:\n+|$)/, '').trim();
  if (!text) return [];
  const messages = []; let lines = [], fence = false;
  for (const line of text.split('\n')) {
    if (/^\s*```/.test(line)) fence = !fence;
    if (!fence && /^(?:\[\d{1,4}[/-]\d{1,2}[/-]\d{1,4}(?:,|\s)|(?:user|assistant|system|human|model|tool|developer):\s)/i.test(line) && lines.length) {
      if (lines.join('\n').trim()) messages.push(lines.join('\n').trim());
      lines = [];
    }
    lines.push(line);
  }
  if (lines.join('\n').trim()) messages.push(lines.join('\n').trim());
  return messages;
}

let detailItem = null, detailReturnFocus = null;
function inspectorMarkdown(text) {
  const inline = value => esc(value).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  let code = false, buffer = [], html = '', list = false;
  for (const line of String(text || '').split('\n')) {
    if (/^\s*```/.test(line)) { if (list) { html += '</ul>'; list = false; } if (code) { html += '<pre><code>' + esc(buffer.join('\n')) + '</code></pre>'; buffer = []; } code = !code; continue; }
    if (code) { buffer.push(line); continue; }
    const bullet = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)/);
    if (bullet) { if (!list) html += '<ul>'; list = true; html += '<li>' + inline(bullet[1]) + '</li>'; continue; }
    if (list) { html += '</ul>'; list = false; }
    const heading = line.match(/^#{1,6}\s+(.*)/);
    if (heading) html += '<h4>' + inline(heading[1]) + '</h4>';
    else if (line.trim()) html += '<p>' + inline(line) + '</p>';
  }
  if (list) html += '</ul>';
  if (code) html += '<pre><code>' + esc(buffer.join('\n')) + '</code></pre>';
  return html;
}
async function openDetail(id) {
  detailId = id;
  try {
    const it = await api(`/api/memories/${id}`);
    if (detailId !== id) return;
    detailItem = it;
    if (!document.getElementById('detail-panel').classList.contains('open')) detailReturnFocus = document.activeElement;
    const conversation = it.type === 'conversation';
    const title = String(it.content || '').match(/^#\s+([^\n]+)/);
    document.getElementById('detail-panel-title').textContent = title ? title[1] : String(it.content || '').split('\n')[0].slice(0, 100) || 'Untitled memory';
    document.getElementById('d-kind').textContent = conversation ? 'Conversation reader' : 'Memory inspector';
    const date = new Date(it.created_at);
    document.getElementById('d-meta').textContent = `${sourceLabel(it.source)} · ${isNaN(date) ? it.created_at : date.toLocaleDateString(undefined, {year:'numeric', month:'short', day:'numeric'})}`;
    document.getElementById('d-chips').innerHTML = [...((it.projects || []).length ? it.projects.map(p => p.name) : ['No project']), it.type, ...(it.tags || [])].map(tag => `<span>${esc(tag)}</span>`).join('');
    document.getElementById('d-id').textContent = it.id;
    document.getElementById('d-created').textContent = it.created_at;
    document.getElementById('d-ref').textContent = it.source_ref || 'Saved directly in EleSync';
    document.getElementById('d-raw').textContent = it.content;
    document.getElementById('d-details').open = false;
    document.getElementById('conversation-search').value = '';
    const content = String(it.content || '').replace(/^#[^\n]*(?:\n+|$)/, '').trim();
    const preview = conversation ? (conversationMessages(it.content)[0] || '') : content;
    document.getElementById('d-preview').textContent = preview.replace(/^(user|assistant|human|model):\s*/i, '').replace(/[#*`]/g, '').replace(/\s+/g, ' ').slice(0, 220) + (preview.length > 220 ? '…' : '');
    document.getElementById('d-context-content').innerHTML = conversation ? '<p class="inspector-muted">This item saves the conversation itself. No separate memory or summary is stored.</p>' : inspectorMarkdown(content);
    conversationReader = conversation ? {messages: conversationMessages(it.content), page:0, matches:null} : null;
    editDetail(false);
    if (conversation) renderConversationReader();
    const panel = document.getElementById('detail-panel');
    panel.classList.add('open'); panel.inert = false; panel.setAttribute('aria-hidden', 'false');
    document.getElementById('detail-backdrop').hidden = false;
    document.body.classList.add('inspector-open');
    panel.querySelector('.detail-body').scrollTop = 0;
    requestAnimationFrame(() => { if (detailId === id) panel.querySelector('.detail-close').focus({preventScroll:true}); });
  } catch (error) { if (detailId === id) { detailId = null; toast(error.message, 'err'); } }
}
function editDetail(editing) {
  if (!detailItem) return;
  ['d-overview','d-context','d-details'].forEach(id => { document.getElementById(id).hidden = editing; });
  // A short memory is its own preview; showing both repeated it word for word.
  if (!editing && detailItem.type !== 'conversation'
      && String(detailItem.content || '').replace(/^#[^\n]*(?:\n+|$)/, '').trim().length <= 220) {
    document.getElementById('d-overview').hidden = true;
  }
  document.getElementById('conversation-reader').hidden = editing || detailItem.type !== 'conversation';
  document.getElementById('d-editor').hidden = !editing;
  document.getElementById('d-edit').hidden = editing;
  document.getElementById('d-save').hidden = !editing;
  document.getElementById('d-cancel').hidden = !editing;
  document.getElementById('d-content').value = detailItem.content;
  document.getElementById('d-source').value = detailItem.source;
  document.getElementById('d-type').value = detailItem.type;
  document.getElementById('d-tags').value = (detailItem.tags || []).join(', ');
  if (editing) document.getElementById('d-content').focus();
}
async function copyDetail() { try { await navigator.clipboard.writeText(detailItem.content); toast('Copied'); } catch (_) { toast('Could not copy. Open Details & raw content to select the text.', 'err'); } }
function renderConversationReader() {
  const r = conversationReader; if (!r) return;
  const list = r.matches || r.messages, size = 120, start = r.page * size;
  document.getElementById('conversation-reader-results').innerHTML = list.slice(start, start + size).map((message, index) => {
    const match = message.match(/^(user|assistant|system|human|model|tool|developer):\s*/i);
    const role = match ? match[1].toLowerCase() : 'message';
    const user = role === 'user' || role === 'human';
    const label = user ? 'You' : role === 'assistant' || role === 'model' ? detailItem.source : role;
    return `<article class="conversation-message ${user ? 'is-user' : ''}"><header><strong>${esc(label)}</strong><span>${start + index + 1}</span></header><div class="inspector-prose">${inspectorMarkdown(match ? message.slice(match[0].length) : message)}</div></article>`;
  }).join('') || '<p class="inspector-muted">No messages match your search.</p>';
  document.getElementById('conversation-reader-count').textContent = `${list.length.toLocaleString()} ${list.length === 1 ? 'message' : 'messages'}`;
  document.getElementById('conversation-reader-page').textContent = `Page ${r.page + 1} of ${Math.max(1, Math.ceil(list.length / size))}`;
  document.getElementById('conversation-prev').disabled = r.page === 0;
  document.getElementById('conversation-next').disabled = start + size >= list.length;
}
function pageConversationReader(change) { if (!conversationReader) return; const total = Math.max(1, Math.ceil((conversationReader.matches || conversationReader.messages).length / 120)); conversationReader.page = Math.max(0, Math.min(total - 1, conversationReader.page + change)); renderConversationReader(); document.getElementById('conversation-reader').scrollIntoView({block:'start'}); }
function searchConversationReader() { if (!conversationReader) return; const query = document.getElementById('conversation-search').value.trim().toLowerCase(); conversationReader.matches = query ? conversationReader.messages.filter(message => message.toLowerCase().includes(query)) : null; conversationReader.page = 0; renderConversationReader(); }
function closeDetail() {
  const panel = document.getElementById('detail-panel');
  panel.classList.remove('open'); panel.inert = true; panel.setAttribute('aria-hidden', 'true');
  document.getElementById('detail-backdrop').hidden = true;
  document.body.classList.remove('inspector-open');
  document.querySelectorAll('.memory-card').forEach(el => el.classList.remove('selected'));
  detailId = null; detailItem = null;
  const target = detailReturnFocus && detailReturnFocus.isConnected && detailReturnFocus !== document.body ? detailReturnFocus : document.getElementById('nav-browse');
  target.focus({preventScroll:true});
}
document.addEventListener('keydown', event => {
  const panel = document.getElementById('detail-panel');
  if (!panel.classList.contains('open')) return;
  if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); closeDetail(); }
  else if (event.key === 'Tab') {
    const items = [...panel.querySelectorAll('button,input,textarea,summary,[tabindex="0"]')].filter(el => !el.disabled && el.getClientRects().length);
    const first = items[0], last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  } else if (!['INPUT','TEXTAREA'].includes(event.target.tagName)) event.stopImmediatePropagation();
}, true);
async function saveDetail() {
  if (!detailId) return;
  const savedId = detailId;
  try {
    const body = {
      content: document.getElementById('d-content').value,
      source: document.getElementById('d-source').value,
      type: document.getElementById('d-type').value,
      tags: document.getElementById('d-tags').value.split(',').map(t => t.trim()).filter(Boolean),
    };
    await api(`/api/memories/${savedId}`, { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
    toast('Saved');
    if (detailId === savedId) await openDetail(savedId);
    if (currentPage === 'browse') loadBrowse(0);
  } catch (e) { toast(e.message, 'err'); }
}
async function deleteDetail() {
  if (!detailId || !await confirmDialog({ title:'Delete this memory?', body:'This removes it from your vault.', confirmLabel:'Delete memory', danger:true })) return;
  try {
    await api(`/api/memories/${detailId}`, { method:'DELETE' });
    closeDetail();
    toast('Deleted');
    if (currentPage === 'browse') loadBrowse(0);
    if (currentPage === 'overview') loadOverview();
  } catch (e) { toast(e.message, 'err'); }
}

/* ──────────────── Encrypted offline drafts ──────────────── */
const OFFLINE_DRAFTS_KEY = 'elesync-offline-drafts-v2';
const OFFLINE_KEY_DB = 'elesync-offline-key-v1';
function idbRequest(request) { return new Promise((resolve, reject) => { request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); }); }
function draftKey() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(OFFLINE_KEY_DB, 1);
    request.onupgradeneeded = () => request.result.createObjectStore('keys');
    request.onsuccess = async () => {
      try {
        const db = request.result;
        const readTx = db.transaction('keys', 'readonly');
        let key = await idbRequest(readTx.objectStore('keys').get('draft-key'));
        if (!key) {
          key = await crypto.subtle.generateKey({name:'AES-GCM', length:256}, false, ['encrypt','decrypt']);
          const writeTx = db.transaction('keys', 'readwrite');
          writeTx.objectStore('keys').put(key, 'draft-key');
        }
        resolve(key);
      } catch (error) { reject(error); }
    };
    request.onerror = () => reject(request.error);
  });
}
function base64(bytes) { let text = ''; bytes.forEach(byte => { text += String.fromCharCode(byte); }); return btoa(text); }
function bytes(value) { return Uint8Array.from(atob(value), char => char.charCodeAt(0)); }
async function readOfflineDrafts() {
  try {
    const raw = localStorage.getItem(OFFLINE_DRAFTS_KEY);
    if (!raw) return [];
    const saved = JSON.parse(raw);
    if (Array.isArray(saved)) return saved; // migrate the v1 queue on first read
    const key = await draftKey();
    const plain = await crypto.subtle.decrypt({name:'AES-GCM', iv:bytes(saved.iv)}, key, bytes(saved.data));
    return JSON.parse(new TextDecoder().decode(plain));
  } catch (error) {
    throw new Error('Encrypted offline drafts could not be opened. They were left untouched.');
  }
}
async function writeOfflineDrafts(drafts) {
  if (!drafts.length) { localStorage.removeItem(OFFLINE_DRAFTS_KEY); updateOfflineStatus(0); return; }
  try {
    const key = await draftKey();
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const encrypted = await crypto.subtle.encrypt({name:'AES-GCM', iv}, key, new TextEncoder().encode(JSON.stringify(drafts.slice(-50))));
    localStorage.setItem(OFFLINE_DRAFTS_KEY, JSON.stringify({iv:base64(iv), data:base64(new Uint8Array(encrypted))}));
  } catch (error) {
    throw new Error('Offline draft encryption is unavailable. The draft was not stored.');
  }
  updateOfflineStatus(drafts.length);
}
async function saveOfflineDraft(body) {
  const drafts = await readOfflineDrafts();
  drafts.push({...body, saved_at: new Date().toISOString()});
  await writeOfflineDrafts(drafts);
}
function updateOfflineStatus(count = 0) {
  const el = document.getElementById('offline-status');
  if (el) el.textContent = count ? `${count} saved offline` : '';
}
async function flushOfflineDrafts() {
  let drafts;
  try { drafts = await readOfflineDrafts(); }
  catch (error) { updateOfflineStatus(0); return; }
  if (!drafts.length || !navigator.onLine) { updateOfflineStatus(drafts.length); return; }
  // A draft can outlive its project. Save it without one rather than retry a
  // "project not found" refusal forever.
  let known = null;
  try { known = new Set(((await api('/api/projects')).projects || []).map(project => project.id)); }
  catch (_) { /* unreachable: every save below will wait for next time too */ }
  const remaining = [];
  for (const draft of drafts) {
    const body = known && draft.project_id && !known.has(draft.project_id) ? {...draft, project_id: null} : draft;
    try { await api('/api/memories', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}); }
    catch (_) { remaining.push(draft); }
  }
  await writeOfflineDrafts(remaining);
  if (drafts.length !== remaining.length) { toast('Offline memories synced'); loadOverview(); }
}
function clearOfflineDrafts() { localStorage.removeItem(OFFLINE_DRAFTS_KEY); updateOfflineStatus(0); toast('Offline drafts cleared'); }

/* ──────────────── Add memory ──────────────── */
async function loadAddProjects() {
  const select = document.getElementById('add-project');
  if (!select) return;
  const none = '<option value="">Entire workspace (no project)</option>';
  try {
    const projects = (await api('/api/projects')).projects || [];
    forgetMissingActiveProject(projects);
    select.innerHTML = none + projects.map(p => `<option value="${_gEsc(p.id)}">${_gEsc(p.name || 'Untitled project')}</option>`).join('');
  } catch (_) {
    // Offline: keep the remembered project so a draft saved now still files there.
    const id = activeProjectId();
    select.innerHTML = none + (id ? `<option value="${_gEsc(id)}">Current project</option>` : '');
  }
  select.value = activeProjectId();
}

// Only a save that never reached EleSync is worth keeping to retry: no
// connection, the phone link's gateway not reaching this computer, a busy or
// failing server, or an expired sign-in (it syncs after signing in again).
function worthRetryingLater(error) {
  return Boolean(error.network) || [401, 408, 429].includes(error.status) || error.status >= 500;
}

async function addMemory() {
  const content = document.getElementById('add-content').value.trim();
  if (!content) { toast('Content is required', 'err'); return; }
  const project = document.getElementById('add-project');
  const body = {
    content,
    source: document.getElementById('add-source').value,
    type: document.getElementById('add-type').value,
    tags: document.getElementById('add-tags').value.split(',').map(t => t.trim()).filter(Boolean),
    project_id: (project ? project.value : activeProjectId()) || null,
  };
  try {
    await api('/api/memories', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
    toast('Memory saved');
    document.getElementById('add-content').value = '';
    document.getElementById('add-tags').value = '';
  } catch (e) {
    if (!worthRetryingLater(e)) {
      // EleSync answered and refused it. Queueing it as "offline" would retry a
      // save that can never succeed; keep the text here and say why instead.
      toast('Not saved: ' + e.message, 'err');
      return;
    }
    try {
      await saveOfflineDraft(body);
      toast('Saved offline. It will sync when EleSync is reachable.');
    } catch (storageError) {
      toast(storageError.message + ' Keep this page open or copy your text before leaving.', 'err');
    }
  }
}

function startVoiceInput() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    toast('Voice input is not supported in this browser.', 'err');
    return;
  }

  const btn = document.getElementById('voice-input-btn');
  const status = document.getElementById('voice-status');
  const textarea = document.getElementById('add-content');

  if (btn.classList.contains('recording')) {
    // Already recording - we can't easily stop it manually with the simple API
    // without tracking the instance, so we just let it be.
    // In a more robust implementation we'd track the recognition object.
    return;
  }

  const recognition = new SpeechRecognition();
  recognition.lang = 'en-US';
  recognition.interimResults = false;
  recognition.maxAlternatives = 1;

  recognition.onstart = () => {
    btn.classList.add('recording');
    status.textContent = 'Listening…';
  };

  recognition.onresult = (event) => {
    const transcript = event.results[0][0].transcript;
    if (textarea.value) {
      textarea.value += ' ' + transcript;
    } else {
      textarea.value = transcript;
    }
  };

  recognition.onerror = (event) => {
    const detail = event.error === 'network'
      ? 'Your browser speech service is unavailable. Check internet access or type the memory instead.'
      : event.error === 'not-allowed'
        ? 'Microphone access was blocked. Allow microphone access or type the memory instead.'
        : 'Voice input could not start (' + event.error + '). Type the memory instead.';
    toast(detail, 'err');
  };

  recognition.onend = () => {
    btn.classList.remove('recording');
    status.textContent = 'Voice input';
  };

  recognition.start();
}

/* ──────────────── Import ──────────────── */

/* ──────────────── Export ──────────────── */
async function exportVault() {
  try {
    const data = await api('/api/export');
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url;
    a.download = `elesync-export-${new Date().toISOString().slice(0, 10)}.json`;
    a.click(); URL.revokeObjectURL(url);
    toast(`Exported ${data.total} memories as JSON`);
  } catch (e) { toast(e.message, 'err'); }
}

async function exportVaultCSV() {
  try {
    const data = await api('/api/export');
    const rows = [['ID', 'Date', 'Source', 'Type', 'Content']];
    for (const mem of data.memories) {
      rows.push([
        mem.id,
        mem.created_at?.slice(0, 10) || '',
        mem.source || '',
        mem.type || '',
        (mem.content || '').replace(/"/g, '""')
      ]);
    }
    const csv = rows.map(row => row.map(cell => `"${cell}"`).join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url;
    a.download = `elesync-export-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click(); URL.revokeObjectURL(url);
    toast(`Exported ${data.total} memories as CSV`);
  } catch (e) { toast(e.message, 'err'); }
}

/* ──────────────── Conflicts ──────────────── */
async function loadConflicts() {
  const el = document.getElementById('conflicts-content');
  await withLoader(el, [90, 75, 82], 'Conflicts', async function (signal) {
  const data = await api('/api/conflicts', { signal });
  if (!data.conflicts.length) {
    el.innerHTML = '<div class="empty-state"><div class="empty-icon">' + icon('check-circle') + '</div><div class="empty-text">No conflicts</div><div class="empty-sub">Your vault is consistent. No memories contradict each other.</div></div>';
    document.getElementById('topbar-sub').textContent = 'Clean';
    return;
  }
  document.getElementById('topbar-sub').textContent = `${data.total} cluster(s)`;
  el.innerHTML = data.conflicts.map((c, ci) =>
    `<div class="conflict-cluster">` +
    `<div class="conflict-head"><div class="conflict-title">Conflict ${ci + 1} of ${data.total}</div><div class="conflict-cid">${_gEsc(String(c.cluster_id).slice(0, 8))}</div></div>` +
    c.items.map((it, ii) =>
      `<label class="conflict-entry"><input type="radio" name="cf-${ci}" value="${_gEsc(it.id)}" ${ii === 0 ? 'checked' : ''}>` +
      `<div><div class="ce-content">${esc(it.content)}</div><div class="ce-meta">${_gEsc(sourceLabel(it.source))} · ${_gEsc((it.created_at || '').slice(0, 10))} · ${_gEsc(String(it.id).slice(0, 8))}</div></div></label>`
    ).join('') +
    `<div class="conflict-actions">` +
    `<button class="btn btn-sm btn-primary" data-cf="pick" data-ci="${ci}" data-cid="${_gEsc(c.cluster_id)}">Keep selected</button>` +
    `<button class="btn btn-sm" data-cf="newest" data-ci="${ci}" data-cid="${_gEsc(c.cluster_id)}">Keep newest</button>` +
    `<button class="btn btn-sm" data-cf="longest" data-ci="${ci}" data-cid="${_gEsc(c.cluster_id)}">Keep longest</button>` +
    `</div></div>`
  ).join('');
  el.querySelectorAll('[data-cf]').forEach(function (b) {
    b.addEventListener('click', function () {
      const ci = Number(b.dataset.ci), cid = b.dataset.cid, how = b.dataset.cf;
      if (how === 'pick') resolveConflict(ci, cid); else resolveAuto(ci, cid, how);
    });
  });
  }, 'conflicts');
}

async function resolveConflict(ci, cid) {
  const sel = document.querySelector(`input[name="cf-${ci}"]:checked`);
  if (!sel) return;
  try {
    const r = await api('/api/conflicts/resolve', { method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ cluster_id: cid, choose_id: sel.value }) });
    toast(r.result || 'Resolved'); loadConflicts(); loadOverview();
  } catch (e) { toast(e.message, 'err'); }
}
async function resolveAuto(ci, cid, strategy) {
  try {
    const r = await api('/api/conflicts/resolve', { method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ cluster_id: cid, strategy }) });
    toast(r.result || 'Resolved'); loadConflicts(); loadOverview();
  } catch (e) { toast(e.message, 'err'); }
}

/* ──────────────── Scopes ──────────────── */
async function loadScopes() {
  const el = document.getElementById('scopes-content');
  const data = await api('/api/scopes');
  const entries = Object.entries(data.scopes || {}).sort((a, b) => b[1] - a[1]);
  const maxC = Math.max(...entries.map(e => e[1]), 1);
  if (!entries.length) {
    el.innerHTML = '<div class="empty-state"><div class="empty-icon">' + icon('globe') + '</div><div class="empty-text">No scopes configured</div><div class="empty-sub">All memories are in the default “general” compartment, visible to every AI client.</div></div>';
    return;
  }
  document.getElementById('topbar-sub').textContent = `${entries.length} compartment(s)`;
  const icons = { general:'globe', work:'briefcase', personal:'house', claude:'chat-circle', chatgpt:'robot' };
  el.innerHTML = entries.map(([name, count]) =>
    `<div class="scope-card-lg"><div class="scope-icon-lg">${icon(icons[name] || 'folder')}</div>` +
    `<div class="scope-info"><div class="scope-info-name">${esc(name)}</div>` +
    `<div class="scope-info-count">${count} memor${count === 1 ? 'y' : 'ies'}</div></div>` +
    `<div class="scope-bar-lg"><div class="scope-fill-lg" style="width:${(count/maxC*100).toFixed(1)}%"></div></div></div>`
  ).join('');
}

/* ──────────────── USB ──────────────── */
async function usbBrowse(path) {
  const el = document.getElementById('usb-folders');
  el.textContent = 'Loading folders…';
  try {
    const data = await api(`/api/usb/folders?path=${encodeURIComponent(path)}`);
    el.replaceChildren();
    const heading = document.createElement('p');
    heading.textContent = data.path || 'Choose a drive on this computer';
    el.appendChild(heading);
    function button(label, action) {
      const btn = document.createElement('button');
      btn.className = 'btn btn-sm'; btn.textContent = label;
      btn.addEventListener('click', action); el.appendChild(btn);
    }
    if (data.path) {
      button('Use this folder', () => {
        document.getElementById('usb-manual-path').value = data.path;
        usbSelect(0, data.path);
      });
      button('Up', () => usbBrowse(data.parent));
    }
    data.folders.forEach(folder => button(folder, () => usbBrowse(folder)));
    if (data.truncated) el.append('Showing the first 200 folders. You can also enter a full path.');
  } catch (e) { el.textContent = e.message; }
}
async function usbRefresh() {
  const el = document.getElementById('usb-drives');
  document.getElementById('usb-status').innerHTML = '';
  try {
    const data = await api('/api/usb/list');
    if (!data.drives || !data.drives.length) {
      el.innerHTML = `<div class="empty-state"><div class="empty-icon">${icon('usb')}</div><div class="empty-text">${_gEsc(data.error || 'No removable drives detected. Plug in a drive and refresh, or browse folders.')}</div></div>`;
      return;
    }
    /* A drive's `path` and `label` come from the VOLUME NAME of whatever stick is
       plugged in (usb.py: label = Path(mount_point).name). That is attacker-
       controlled: a volume called  x'); alert(1); //  used to break straight out
       of the single-quoted JS string this handler was built from, and one called
       <img src=x onerror=...> went raw into innerHTML further down. The dashboard
       holds full read/write/export over the vault, so that was total compromise
       from merely plugging in someone else's USB stick — a first-class use case
       for this feature.
       Fix the class, not the character: user data never goes into an executable
       context. It rides in a data-* attribute and is read back via .dataset,
       which hands us the decoded string with no JS-string parsing in between. */
    el.innerHTML = data.drives.map((d, i) =>
      `<div class="usb-drive" data-idx="${i}" data-path="${_gEsc(d.path)}">` +
      `<div class="usb-icon">${icon('hard-drive')}</div><div class="usb-info"><div class="usb-name">${_gEsc(d.label || d.path)}</div>` +
      `<div class="usb-meta">${_gEsc(d.path)} · ${_gEsc(d.size_gb)} GB</div></div>` +
      `<div class="usb-badge ${d.has_vault ? 'vault' : 'no-vault'}">${d.has_vault ? 'Vault' : 'No vault'}</div></div>`
    ).join('');
    el.querySelectorAll('.usb-drive').forEach(function (node) {
      node.addEventListener('click', function () {
        usbSelect(Number(node.dataset.idx), node.dataset.path);
      });
    });
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><div class="empty-text text-danger">${esc(e.message)}</div></div>`;
  }
}
async function usbSelect(i, path) {
  const el = document.getElementById('usb-status');
  try {
    const data = await api(`/api/usb/status/${encodeURIComponent(path)}`);
    /* `drive` and `vault_dir` in this payload are built from the mount path, i.e.
       from the volume name. They were interpolated here with NO escaping at all. */
    el.innerHTML = `<div class="card"><div class="card-title">Selected destination</div>` +
      `<p class="break-anywhere">${_gEsc(data.drive)}</p>` +
      `<p>${_gEsc(data.size_gb)} GB available · ${data.vault_present ? 'Existing vault found' : 'Ready for a new backup'}</p>` +
      `<button class="btn btn-sm btn-primary" id="usb-act" data-act="install" data-path="${_gEsc(path)}">Copy &amp; encrypt here</button>` +
      (data.vault_present ? `<button class="btn btn-sm" id="usb-access">Access existing vault</button>` : '') +
      `</div>`;
    const access = document.getElementById('usb-access');
    if (access) access.addEventListener('click', () => vaultSwitchTo(data.vault_dir));
    const act = document.getElementById('usb-act');
    if (act) act.addEventListener('click', function () {
      const p = act.dataset.path;
      if (act.dataset.act === 'switch') vaultSwitchTo(p); else usbInstall(p);
    });
  } catch (e) { el.innerHTML = `<div class="text-danger">${_gEsc(e.message)}</div>`; }
}
function usbInstall(path) {
  document.getElementById('usb-install-path').value = path;
  document.getElementById('usb-install-destination').textContent = path;
  document.getElementById('usb-install-pass').value = '';
  document.getElementById('usb-install-pass2').value = '';
  document.getElementById('usb-install-err').style.display = 'none';
  document.getElementById('usb-install-dialog').classList.add('open');
  document.getElementById('usb-install-pass').focus();
}
function usbInstallClose() {
  document.getElementById('usb-install-dialog').classList.remove('open');
  document.getElementById('usb-install-pass').value = '';
  document.getElementById('usb-install-pass2').value = '';
}
async function usbInstallConfirm() {
  const path = document.getElementById('usb-install-path').value;
  const pass = document.getElementById('usb-install-pass').value;
  const pass2 = document.getElementById('usb-install-pass2').value;
  const portable = document.getElementById('usb-install-app').checked;
  const errEl = document.getElementById('usb-install-err');
  const btn = document.getElementById('usb-install-btn');
  if (!pass) { errEl.textContent = 'A passphrase is required. USB vaults are always encrypted.'; errEl.style.display = 'block'; return; }
  if (pass !== pass2) { errEl.textContent = 'Passphrases do not match.'; errEl.style.display = 'block'; return; }
  errEl.style.display = 'none';
  if (btn) { btn.disabled = true; btn.textContent = portable ? 'Installing…' : 'Encrypting…'; }
  try {
    const result = await api('/api/usb/install', { method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ drive: path, passphrase: pass, mode: portable ? 'portable' : 'export' }), timeoutMs: portable ? 300000 : 60000 });
    usbInstallClose();
    await usbRefresh();
    document.getElementById('usb-status').innerHTML = portable
      ? `<div class="card"><div class="card-title">Portable EleSync installed</div><p>${_gEsc(result.count)} memories copied. Double-click EleSync.exe whenever you plug in the drive.</p><p class="break-anywhere">${_gEsc(result.launcher)}</p></div>`
      : `<div class="card"><div class="card-title">Encrypted backup saved</div><p>${_gEsc(result.count)} memories copied, with project data and custom templates.</p><p class="break-anywhere">${_gEsc(result.backup_path)}</p></div>`;
  } catch (e) {
    errEl.textContent = e.message; errEl.style.display = 'block';
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Install & encrypt'; }
  }
}
function vaultSwitchTo(path) { document.getElementById('vault-switch-path').value = path; vaultDialog(); }

/* ──────────────── Vault switch ──────────────── */
/* Activity stream */
function timeAgo(iso) {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (isNaN(t)) return '';
  let s = Math.floor((Date.now() - t) / 1000);
  if (s < 0) s = 0;
  if (s < 60) return s + 's';
  const m = Math.floor(s/60); if (m < 60) return m + 'm';
  const h = Math.floor(m/60); if (h < 24) return h + 'h';
  return Math.floor(h/24) + 'd';
}
/* ──────────────── Presenter keys (`ele demo` only) ────────────────
   Deliberately invisible so a recording shows only the product. Keys 1–9 run
   the scripted steps; backtick toggles a cheat sheet for rehearsal. */
let _demoSteps = [];

async function setupDemoKeys() {
  let info;
  try { info = await api('/api/demo'); } catch (_) { return; }
  if (!info || !info.enabled) return;
  _demoSteps = info.steps || [];
  document.addEventListener('keydown', event => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    // Only text entry swallows the keys; a checkbox or button that still has
    // focus after a click (e.g. unticking a suggested project) must not.
    const target = event.target;
    const typing = target && (target.isContentEditable || ['TEXTAREA', 'SELECT'].includes(target.tagName)
      || (target.tagName === 'INPUT' && !['checkbox', 'radio', 'button', 'submit', 'reset'].includes(target.type)));
    if (typing) return;
    if (event.key === '`') { event.preventDefault(); toggleDemoCheatsheet(); return; }
    const step = _demoSteps.find(s => s.key === event.key);
    if (step) { event.preventDefault(); runDemoStep(step.id); }
  });
}

function toggleDemoCheatsheet() {
  const existing = document.getElementById('demo-cheatsheet');
  if (existing) { existing.remove(); return; }
  const sheet = document.createElement('div');
  sheet.id = 'demo-cheatsheet';
  sheet.className = 'demo-cheatsheet';
  sheet.innerHTML = _demoSteps.map(s => `<button type="button" data-demo-step="${_gEsc(s.id)}"><kbd>${_gEsc(s.key)}</kbd>${_gEsc(s.label)}</button>`).join('');
  sheet.querySelectorAll('[data-demo-step]').forEach(button => button.addEventListener('click', () => runDemoStep(button.dataset.demoStep)));
  document.body.appendChild(sheet);
}

/* Owner actions (granting and revoking access) are played through the real
   AI Access controls, so a recording shows them being done rather than only
   announcing them. They go through the same endpoints a click does. */
const DEMO_OWNER_STEPS = {
  'connect': [['grant', 'claude'], ['grant', 'chatgpt']],
  'revoke-chatgpt': [['revoke', 'chatgpt']],
  'connect-grok': [['grant', 'grok']],
};
let _demoBusy = false;
const demoPause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function demoPress(el, hold = 450) {
  if (!el) return;
  el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  await demoPause(350);
  el.classList.add('demo-press');
  await demoPause(hold);
  el.classList.remove('demo-press');
  el.click();
}

async function demoOpenProjectAccess() {
  const projects = (await api('/api/projects')).projects || [];
  const project = projects.find(p => /kyoto/i.test(p.name));
  if (!project) return null;
  localStorage.setItem('elesync-active-project', project.id);
  if (currentPage !== 'projects' || document.getElementById('project-detail')?.dataset.projectId !== project.id) {
    go('projects');
    await openProject(project.id);
    await demoPause(500);
  }
  const tab = document.querySelector('[data-project-tab="access"]');
  if (tab && !tab.classList.contains('active')) { await demoPress(tab, 250); await demoPause(400); }
  return project;
}

async function demoWaitFor(selector, timeout = 3000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const el = document.querySelector(selector);
    if (el) return el;
    await demoPause(100);
  }
  return null;
}

async function demoGrantDirect(projectId, provider) {
  await api('/api/projects/' + encodeURIComponent(projectId) + '/access', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider, can_read: true, can_write: true, can_read_conversations: false }) });
  toast(accessSummary(connectClientMeta(provider).label, true, true));
  loadProjectAccess(projectId);
}

async function demoHasAccess(projectId, provider) {
  const rows = (await api('/api/projects/' + encodeURIComponent(projectId) + '/access')).access || [];
  return rows.some(row => row.provider === provider && row.status === 'active' && row.can_write);
}

async function demoGrant(projectId, provider) {
  const select = document.getElementById('project-access-provider');
  // The form is only the visible route; the grant itself must always land.
  if (!select) { await demoGrantDirect(projectId, provider); return; }
  select.scrollIntoView({ block: 'center', behavior: 'smooth' });
  await demoPause(400);
  select.classList.add('demo-press');
  select.value = provider;
  select.dispatchEvent(new Event('change', { bubbles: true }));
  await demoPause(500);
  select.classList.remove('demo-press');
  for (const [id, want] of [['project-access-read', true], ['project-access-write', true], ['project-access-conversations', false]]) {
    const box = document.getElementById(id);
    if (box && box.checked !== want) await demoPress(box, 250);
  }
  await demoPause(250);
  await demoPress(document.querySelector('[data-project-action="grant"]'));
  await demoWaitFor(`[data-revoke-provider="${provider}"]`);
  if (!await demoHasAccess(projectId, provider)) await demoGrantDirect(projectId, provider);
  await demoPause(900);
}

async function demoRevoke(provider) {
  const button = await demoWaitFor(`[data-revoke-provider="${provider}"]`);
  if (!button) return;
  await demoPress(button, 600);
  const confirmButton = await demoWaitFor('[data-confirm-ok]');
  await demoPause(1400);
  await demoPress(confirmButton, 400);
  await demoWaitFor(`[data-restore-provider="${provider}"]`);
}

async function runDemoOwnerStep(id) {
  const project = await demoOpenProjectAccess();
  if (!project) return false;
  for (const [action, provider] of DEMO_OWNER_STEPS[id]) {
    if (action === 'grant') await demoGrant(project.id, provider);
    else await demoRevoke(provider);
  }
  return true;
}

async function runDemoStep(id) {
  if (_demoBusy) return;
  _demoBusy = true;
  try { await runDemoStepNow(id); } finally { _demoBusy = false; }
}

async function runDemoStepNow(id) {
  // Without a Kyoto project yet (projects not created after the import), fall
  // back to the server step, which creates one.
  if (DEMO_OWNER_STEPS[id] && await runDemoOwnerStep(id)) return;
  if (id === 'import') {
    go('import');
    const data = await api('/api/demo/history');
    await importFiles([new File([JSON.stringify(data)], 'conversations.json', { type: 'application/json' })]);
    document.getElementById('import-discovery')?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    return;
  }
  let result;
  try {
    result = await api('/api/demo/step', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ step: id }) });
  } catch (error) {
    toast(error.message, 'err');
    return;
  }
  if (result.project_id) localStorage.setItem('elesync-active-project', result.project_id);
  if (result.notice) toast(result.notice);
  await pollRelay();
  if (currentPage === 'overview') await loadOverview();
  if (currentPage === 'projects') {
    const openId = document.getElementById('project-detail')?.dataset.projectId;
    if (openId) { loadProjectAccess(openId); loadProjectActivity(openId); }
  }
}

/* ──────────────── Live relay ────────────────
   Polls the audit trail so the dashboard shows, within seconds, when an
   assistant saves to or reads from the vault — the cross-AI handoff made
   visible. The backend records which assistant acted (`ai`) and enriches
   entries with the memory snippet and project. */
const RELAY_AI_LABELS = { phone:'Your phone', claude:'Claude', chatgpt:'ChatGPT', gemini:'Gemini', grok:'Grok', perplexity:'Perplexity', copilot:'Copilot', deepseek:'DeepSeek', cursor:'Cursor', windsurf:'Windsurf' };
const RELAY_QUIET_OPS = new Set(['STATUS', 'HISTORY', 'SCOPE']);
let _relayTimer = null;
let _relaySeen = null;

function relayActor(e) {
  const source = e.memory && e.memory.source && e.memory.source !== 'ai' ? e.memory.source : '';
  const key = String(e.ai || source || '').toLowerCase();
  const label = RELAY_AI_LABELS[key] || (key ? key.replace(/^./, c => c.toUpperCase()) : 'An assistant');
  return { key, label };
}

function relayQuery(detail) {
  const match = /query=(['"])(.*?)\1/.exec(String(detail || ''));
  return match ? match[2] : '';
}

function relayDescribe(e) {
  const op = String(e.op || '').toUpperCase();
  const detail = String(e.detail || '');
  const mem = e.memory || null;
  const firstProject = mem && mem.projects && mem.projects[0];
  if (op === 'ACCESS_DENIED') return { kind:'blocked', verb:'blocked', where: e.project_name || '', text:'Access was revoked' };
  if (detail.indexOf('DENIED') !== -1) return { kind:'blocked', verb:'blocked', where: e.project_name || '', text:'Write not permitted' };
  if (op === 'REMEMBER') return { kind:'write', verb:'saved', where: firstProject || '', text: mem ? mem.preview : '', memoryId: mem && mem.id };
  if (op === 'PROJECT_DOCUMENT') {
    const title = (/title=(['"])(.*?)\1/.exec(detail) || [])[2] || '';
    return { kind:'write', verb:'wrote', where: e.project_name || '', text: title };
  }
  if (op === 'SUPERSEDE') {
    const next = e.replacement;
    const text = next && mem ? relayChange(mem.preview, next.preview) : (mem ? mem.preview : '');
    return { kind:'write', verb:'replaced', where: firstProject || '', text, memoryId: next ? next.id : (mem && mem.id), replacedOwner: mem ? mem.source : '' };
  }
  if (op === 'CAPTURE') return { kind:'write', verb:'saved', where: firstProject || '', text: mem ? mem.preview : '', memoryId: mem && mem.id };
  if (op === 'CONFLICT') return { kind:'conflict', verb:'disagreement', where: firstProject || '', text: mem ? mem.preview : '', memoryId: mem && mem.id };
  if (op === 'RECALL') return { kind:'read', verb:'searched', text: relayQuery(detail) };
  if (op === 'PROJECT_RECALL') return { kind:'read', verb:'searched', where: e.project_name || '', text: relayQuery(detail) };
  if (op === 'PROJECT_CONTEXT') return { kind:'read', verb:'loaded', where: e.project_name || 'a project', text: relayQuery(detail) };
  if (op === 'FORGET') return { kind:'forget', verb:'forgot', text: mem ? mem.preview : '' };
  return { kind:'other', verb: op.toLowerCase().replace(/_/g, ' ') || 'used', text:'' };
}

function relayChange(before, after) {
  // "…at 7pm on October 13." → "…at 8pm…" reads best as just "7pm → 8pm".
  const words = text => String(text || '').split(/\s+/).map(w => w.replace(/^[^\w]+|[^\w]+$/g, '')).filter(Boolean);
  const a = words(before), b = words(after);
  const setA = new Set(a.map(w => w.toLowerCase())), setB = new Set(b.map(w => w.toLowerCase()));
  const removed = a.filter(w => !setB.has(w.toLowerCase()));
  const added = b.filter(w => !setA.has(w.toLowerCase()));
  if (removed.length && added.length && removed.length <= 4 && added.length <= 4) return `${removed.join(' ')} → ${added.join(' ')}`;
  return `${before} → ${after}`;
}

const RELAY_PHONE_ICON = icon('device-mobile');
// The device, not the window width: a desktop tab snapped narrow must still
// announce notes saved from the phone. iPadOS Safari reports a Mac user agent,
// so a touch-capable "Macintosh" counts as a tablet.
const RELAY_ON_PHONE = /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent)
  || Boolean(navigator.userAgentData && navigator.userAgentData.mobile)
  || (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);

function relayInitial(actor) {
  return actor.key === 'phone' ? RELAY_PHONE_ICON : _gEsc(actor.label.slice(0, 1));
}

function relayKey(e) { return [e.ts, e.op, e.mem, e.detail, e.ai].join('|'); }

function relayLabel(source) {
  const key = String(source || '').toLowerCase();
  return RELAY_AI_LABELS[key] || (key && key !== 'ai' ? key.replace(/^./, c => c.toUpperCase()) : 'An assistant');
}

function relayConflictCard(e) {
  const c = e.conflict;
  const card = document.createElement('div');
  card.className = 'toast conflict-card';
  card.setAttribute('role', 'alertdialog');
  card.setAttribute('aria-label', 'Your assistants disagree');
  if (c.cluster_id) card.dataset.cluster = c.cluster_id;
  const newestId = c.items[c.items.length - 1].id;
  const where = (e.memory && e.memory.projects && e.memory.projects[0]) || '';
  const rows = c.items.map(item => {
    const label = item.id === (e.memory && e.memory.id) && e.ai ? relayLabel(e.ai) : relayLabel(item.source);
    const client = CONNECT_CLIENTS.find(x => x.label === label);
    const ago = timeAgo(item.created_at);
    return `<div class="conflict-row${item.id === newestId ? ' is-newest' : ''}">`
      + `<span class="relay-mark" style="--relay-color:${client ? client.color : 'var(--accent)'}">${_gEsc(label.slice(0, 1))}</span>`
      + `<div class="conflict-claim"><strong>${_gEsc(label)}${ago ? ' · ' + _gEsc(ago) + ' ago' : ''}${item.id === newestId ? ' · newest' : ''}</strong><p>${_gEsc(item.preview)}</p></div>`
      + `<button type="button" class="btn btn-sm" data-keep="${_gEsc(item.id)}" data-keep-label="${_gEsc(label)}">Keep</button></div>`;
  }).join('');
  card.innerHTML = `<div class="conflict-head"><strong>Your assistants disagree</strong>${where ? `<small>${_gEsc(where)}</small>` : ''}</div>`
    + rows
    + '<div class="conflict-actions"><button type="button" class="text-button" data-conflict-review>Review all conflicts</button><button type="button" class="text-button" data-conflict-dismiss>Dismiss</button></div>';
  const close = () => { card.style.opacity = '0'; setTimeout(() => card.remove(), 300); };
  card.querySelector('[data-conflict-dismiss]').addEventListener('click', close);
  card.querySelector('[data-conflict-review]').addEventListener('click', () => { close(); go('conflicts'); });
  card.querySelectorAll('[data-keep]').forEach(button => button.addEventListener('click', async () => {
    card.querySelectorAll('button').forEach(b => { b.disabled = true; });
    try {
      await api('/api/conflicts/resolve', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({cluster_id: c.cluster_id, choose_id: button.dataset.keep})});
      const others = c.items.filter(item => item.id !== button.dataset.keep).length;
      card.innerHTML = `<div class="conflict-head"><strong>Kept ${_gEsc(button.dataset.keepLabel)}'s version</strong></div><p class="conflict-done">${others === 1 ? 'The other version is' : 'The other versions are'} kept as history, hidden from recall.</p>`;
      setTimeout(close, 4000);
      if (currentPage === 'overview') loadOverview();
    } catch (error) {
      toast(error.message, 'err');
      card.querySelectorAll('button').forEach(b => { b.disabled = false; });
    }
  }));
  const toasts = document.getElementById('toasts');
  const existing = c.cluster_id && toasts.querySelector(`.conflict-card[data-cluster="${CSS.escape(c.cluster_id)}"]`);
  if (existing) existing.replaceWith(card); else toasts.appendChild(card);
}

function relayNotify(e) {
  if (e.op === 'CONFLICT' && e.conflict && !e.conflict.resolved && e.conflict.cluster_id && (e.conflict.items || []).length >= 2) {
    relayConflictCard(e);
    return;
  }
  const actor = relayActor(e);
  const d = relayDescribe(e);
  const client = CONNECT_CLIENTS.find(c => c.id === actor.key);
  const note = document.createElement('div');
  note.className = 'toast relay-toast relay-' + d.kind;
  note.setAttribute('role', 'status');
  let headline;
  if (d.kind === 'blocked') {
    headline = `${actor.label} was blocked from ${d.where || 'your vault'}`;
  } else if (d.verb === 'replaced') {
    const owner = relayLabel(d.replacedOwner);
    const whose = owner !== 'An assistant' && owner !== actor.label ? `${owner}'s fact` : 'a fact';
    headline = `${actor.label} replaced ${whose}${d.where ? ' in ' + d.where : ''}`;
  } else {
    headline = `${actor.label} ${d.verb}${d.kind === 'write' && d.where ? ' to ' + d.where : d.where ? ' ' + d.where : d.kind === 'read' ? ' your vault' : ''}`;
  }
  note.innerHTML = `<span class="relay-mark" style="--relay-color:${client ? client.color : actor.key === 'phone' ? 'var(--accent2)' : 'var(--accent)'}">${relayInitial(actor)}</span>`
    + `<span class="relay-copy"><strong>${_gEsc(headline)}</strong>${d.text ? `<small>${_gEsc(d.kind === 'read' ? '“' + d.text + '”' : d.text)}</small>` : ''}</span>`;
  if (d.memoryId) {
    note.classList.add('is-clickable');
    note.addEventListener('click', () => openDetail(d.memoryId));
  }
  document.getElementById('toasts').appendChild(note);
  setTimeout(() => { note.style.opacity = '0'; setTimeout(() => note.remove(), 300); }, 6000);
}

function renderRelayStrip(entries, fresh) {
  const el = document.getElementById('relay-strip');
  if (!el) return;
  const described = (entries || []).map(e => ({ e, d: relayDescribe(e) }));
  const lastWrite = described.find(x => x.d.kind === 'write');
  const lastRead = described.find(x => x.d.kind === 'read');
  if (!lastWrite && !lastRead) { el.hidden = true; return; }
  const node = (hit, fallback) => {
    if (!hit) return `<span class="relay-node is-idle">${fallback}</span>`;
    const actor = relayActor(hit.e);
    const client = CONNECT_CLIENTS.find(c => c.id === actor.key);
    return `<span class="relay-node" style="--relay-color:${client ? client.color : actor.key === 'phone' ? 'var(--accent2)' : 'var(--accent)'}"><b>${relayInitial(actor)}</b>${_gEsc(actor.label)}</span>`;
  };
  el.innerHTML = node(lastWrite, 'No saves yet')
    + `<span class="relay-link write"><i></i><small>${lastWrite ? 'saved ' + _gEsc(timeAgo(lastWrite.e.ts)) + ' ago' : 'save'}</small></span>`
    + '<span class="relay-node relay-vault"><b>' + icon('vault') + '</b>Vault</span>'
    + `<span class="relay-link read"><i></i><small>${lastRead ? 'read ' + _gEsc(timeAgo(lastRead.e.ts)) + ' ago' : 'read'}</small></span>`
    + node(lastRead, 'No reads yet');
  el.hidden = false;
  (fresh || []).forEach(e => {
    const kind = relayDescribe(e).kind;
    if (kind !== 'write' && kind !== 'read') return;
    const link = el.querySelector('.relay-link.' + kind);
    if (!link) return;
    link.classList.remove('is-live');
    void link.offsetWidth;
    link.classList.add('is-live');
  });
}

async function pollRelay() {
  // Record the baseline even while hidden, so events that happen before the
  // page is first shown still count as new once it is.
  if (_relaySeen !== null && document.visibilityState !== 'visible') return;
  let data;
  try { data = await api('/api/audit?last=15'); } catch (_) { return; }
  const entries = data.entries || [];
  const keys = entries.map(relayKey);
  if (_relaySeen === null) { _relaySeen = new Set(keys); return; }
  const fresh = entries.filter((e, i) => !_relaySeen.has(keys[i])).reverse();
  _relaySeen = new Set(keys);
  if (!fresh.length) return;
  // A save that immediately replaced or contradicted another fact is told by
  // that richer event alone, not by a second "saved" notification.
  const coveredSaves = new Set(fresh.flatMap(e => {
    if (e.op === 'SUPERSEDE' && e.replacement) return [e.replacement.id];
    if (e.op === 'CONFLICT' && e.memory) return [e.memory.id];
    return [];
  }));
  const notable = fresh.filter(e => !RELAY_QUIET_OPS.has(String(e.op || '').toUpperCase())
    && !(e.op === 'REMEMBER' && e.memory && coveredSaves.has(e.memory.id))
    // The phone already confirmed its own save; the other screens get the relay.
    && !(RELAY_ON_PHONE && e.op === 'CAPTURE'));
  // After the tab was in the background, show only the latest few events.
  notable.slice(-3).forEach(relayNotify);
  if (currentPage === 'overview') {
    await loadOverview();
    renderRelayStrip(entries, fresh);
  } else if (currentPage === 'projects') {
    const openId = document.getElementById('project-detail')?.dataset.projectId;
    if (openId) loadProjectActivity(openId);
  } else if (currentPage === 'connect') {
    loadReadiness();
  }
}

function startRelay() {
  if (_relayTimer) return;
  pollRelay();
  _relayTimer = setInterval(pollRelay, 3000);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') pollRelay(); });
}

async function loadAudit() {
  const el = document.getElementById('audit-stream');
  const status = document.getElementById('audit-status');
  if (!el) return;
  el.innerHTML = '<div class="skel" style="width:80%"></div><div class="skel" style="width:60%"></div><div class="skel" style="width:70%"></div>';
  try {
    const a = await api('/api/audit?last=20');
    if (!a.enabled) {
      if (status) status.textContent = 'off';
      el.innerHTML = '<div class="empty-state compact"><div class="empty-icon">' + icon('pulse') + '</div><div class="empty-text">Audit logging is off.</div><div class="empty-sub">Set ELESYNC_AUDIT=1 to record activity here.</div></div>';
      return;
    }
    if (!a.entries.length) {
      if (status) status.textContent = '';
      renderCompanionActivity([]);
      await renderRecentMemories(el);
      return;
    }
    const rows = collapseRepeats(a.entries).slice(0, 8);
    if (status) status.textContent = rows.length + ' recent';
    el.innerHTML = rows.map(function(row) {
      const e = row.entry, actor = row.actor, d = row.d;
      const cls = d.kind === 'write' ? 'remember' : d.kind === 'read' ? 'recall' : (d.kind === 'forget' || d.kind === 'conflict' || d.kind === 'blocked' ? 'forget' : '');
      return '<div class="audit-row"' + (d.memoryId ? ' data-relay-memory="' + _gEsc(d.memoryId) + '"' : '') + '>'
        + '<span class="audit-op ' + cls + '">' + _gEsc(d.verb) + '</span>'
        + '<span class="audit-client"><strong>' + _gEsc(actor.label) + '</strong>'
        + (d.where ? ' · ' + _gEsc(d.where) : '') + (d.text ? ' · ' + _gEsc(d.text) : '')
        + (row.count > 1 ? ' <span class="audit-count">×' + row.count + '</span>' : '') + '</span>'
        + '<span class="audit-time">' + timeAgo(e.ts) + '</span></div>';
    }).join('');
    el.querySelectorAll('[data-relay-memory]').forEach(row => row.addEventListener('click', () => openDetail(row.dataset.relayMemory)));
    renderRelayStrip(a.entries.slice(0, 8));
    renderCompanionActivity(a.entries.slice(0, 3));
  } catch (err) {
    if (status) status.textContent = '';
    el.innerHTML = '<div class="empty-state">Could not load activity: ' + _gEsc(err.message) + '</div>';
  }
}

// An assistant polling its status wrote the same row again and again; back-to-back
// repeats are one row with a count and the newest time (entries come newest first).
function collapseRepeats(entries) {
  const rows = [];
  for (const entry of entries) {
    const actor = relayActor(entry);
    const d = relayDescribe(entry);
    const key = [actor.label, d.verb, d.where, d.text, d.memoryId].join('\u0000');
    const last = rows[rows.length - 1];
    if (last && last.key === key) { last.count += 1; continue; }
    rows.push({ key, entry, actor, d, count: 1 });
  }
  return rows;
}

function renderCompanionActivity(entries) {
  const el = document.getElementById('companion-activity');
  if (!el) return;
  if (!entries.length) {
    el.innerHTML = '<div class="empty-state compact"><div class="empty-text">No recent activity.</div></div>';
    return;
  }
  el.innerHTML = entries.map(entry => {
    const d = relayDescribe(entry);
    const line = `${relayActor(entry).label} ${d.verb}${d.where ? ' · ' + d.where : ''}`;
    return `<div class="companion-activity-item"><i class="activity-dot cyan"></i><span>${_gEsc(line)}</span><small>${timeAgo(entry.ts)}</small></div>`;
  }).join('');
}

/* Knowledge graph */
/* Escapes for BOTH text nodes and quoted attribute values. Single quotes matter:
   without &#39; this is safe only inside double-quoted attributes, which is a
   footgun for the next person. Prefer _gEsc over esc() anywhere near an attribute
   — esc() round-trips through textContent and escapes NEITHER quote character. */
function _gEsc(x) { return String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

/* Command bar */
const CMDK_ITEMS = [
  { label:'Switch theme (classic / cosmic)', hint:'Action', act:function(){ toggleTheme(); } },
  { label:'New project', hint:'Action', act:function(){ newProject(); } },
  { label:'Find projects in my history', hint:'Action', act:function(){ findProjectsInHistory(); } },
  { label:'Check that my assistants are connected', hint:'Action', act:function(){ go('connect'); } },
  { label:'Continue this project in another AI', hint:'Action', act:function(){ const id = activeProjectId(); if (id) openHandoff(id); else toast('Open a project first', 'err'); } },
  { label:'Select memories to add to a project', hint:'Action', act:function(){ go('browse'); toggleMemorySelect(true); } },
  { label:'Home', hint:'Go', act:function(){ go('overview'); } },
  { label:'Knowledge graph', hint:'Go', act:function(){ go('graph'); } },
  { label:'Memories', hint:'Go', act:function(){ go('browse'); } },
  { label:'New memory', hint:'Go', act:function(){ go('add'); } },
  { label:'Import', hint:'Go', act:function(){ go('import'); } },
  { label:'AI collaborators', hint:'Go', act:function(){ go('connect'); } },
  { label:'Projects', hint:'Go', act:function(){ go('projects'); } },
  { label:'Review conflicts', hint:'Go', act:function(){ go('conflicts'); } },
  { label:'Vault & backups', hint:'Go', act:function(){ go('usb'); } },
  { label:'Use on another device', hint:'Go', act:function(){ go('remote'); } },
  { label:'Switch vault', hint:'Action', act:function(){ closeCmdk(); vaultDialog(); } },
  { label:'Clear offline drafts', hint:'Privacy', act:function(){ clearOfflineDrafts(); } }
];
let cmdkSel = 0, cmdkFiltered = CMDK_ITEMS.slice();
let _cmdkProjects = [], _cmdkMemories = [], _cmdkSeq = 0, _cmdkTimer = null, _cmdkSearching = false;
async function openCmdk() {
  document.getElementById('cmdk-overlay').classList.add('open');
  const i = document.getElementById('cmdk-input'); i.value = ''; cmdkSel = 0;
  _cmdkMemories = []; _cmdkSearching = false;
  renderCmdk(); i.focus();
  try { _cmdkProjects = (await api('/api/projects')).projects || []; renderCmdk(); } catch (_) { _cmdkProjects = []; }
}
function closeCmdk() { document.getElementById('cmdk-overlay').classList.remove('open'); clearTimeout(_cmdkTimer); ++_cmdkSeq; }
function onCmdkInput() {
  cmdkSel = 0;
  const q = document.getElementById('cmdk-input').value.trim();
  clearTimeout(_cmdkTimer);
  const seq = ++_cmdkSeq;
  _cmdkMemories = [];
  _cmdkSearching = q.length >= 2;
  renderCmdk();
  if (!_cmdkSearching) return;
  _cmdkTimer = setTimeout(async function () {
    try {
      const data = await api('/api/memories?q=' + encodeURIComponent(q) + '&limit=6&summary=1');
      if (seq !== _cmdkSeq) return;
      _cmdkMemories = data.items || [];
    } catch (_) {
      if (seq !== _cmdkSeq) return;
      _cmdkMemories = [];
    }
    _cmdkSearching = false;
    renderCmdk();
  }, 180);
}
function cmdkResults(q) {
  const match = function (text) { return String(text || '').toLowerCase().indexOf(q) !== -1; };
  const actions = CMDK_ITEMS.filter(function (it) { return match(it.label); });
  const projects = q ? _cmdkProjects.filter(function (p) { return match(p.name); }).slice(0, 5).map(function (p) {
    return { label: p.name || 'Untitled project', hint: 'Project', act: function () {
      localStorage.setItem('elesync-active-project', p.id); go('projects'); openProject(p.id);
    } };
  }) : [];
  const memories = _cmdkMemories.map(function (m) {
    const title = m.type === 'conversation' ? conversationTitle(m.content, m.source) : memoryTitle(m.content, m.source);
    return { label: title, hint: 'Memory · ' + relayLabel(m.source), act: function () { openDetail(m.id); } };
  });
  return projects.concat(memories, actions);
}
function renderCmdk() {
  const q = document.getElementById('cmdk-input').value.trim().toLowerCase();
  cmdkFiltered = cmdkResults(q);
  if (cmdkSel >= cmdkFiltered.length) cmdkSel = 0;
  const list = document.getElementById('cmdk-list');
  if (!cmdkFiltered.length) {
    list.innerHTML = '<div class="cmdk-empty">' + (_cmdkSearching ? 'Searching…' : 'No matches') + '</div>';
    return;
  }
  list.innerHTML = '';
  cmdkFiltered.forEach(function(it, idx) {
    const d = document.createElement('div');
    d.className = 'cmdk-item' + (idx === cmdkSel ? ' sel' : '');
    d.setAttribute('role', 'option');
    d.setAttribute('aria-selected', idx === cmdkSel ? 'true' : 'false');
    d.innerHTML = '<span class="cmdk-label">' + _gEsc(it.label) + '</span><span class="k">' + _gEsc(it.hint) + '</span>';
    d.addEventListener('click', function(){ closeCmdk(); it.act(); });
    list.appendChild(d);
  });
  if (_cmdkSearching) list.insertAdjacentHTML('beforeend', '<div class="cmdk-empty">Searching memories…</div>');
  list.querySelector('.cmdk-item.sel')?.scrollIntoView({block:'nearest'});
}
function cmdkKey(e) {
  if (e.key === 'ArrowDown') { e.preventDefault(); cmdkSel = Math.min(cmdkSel+1, cmdkFiltered.length-1); renderCmdk(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); cmdkSel = Math.max(cmdkSel-1, 0); renderCmdk(); }
  else if (e.key === 'Enter') { e.preventDefault(); const it = cmdkFiltered[cmdkSel]; if (it) { closeCmdk(); it.act(); } }
  else if (e.key === 'Escape') { closeCmdk(); }
}
document.addEventListener('keydown', function(e) {
  if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) { e.preventDefault(); openCmdk(); }
});

function vaultDialog() { document.getElementById('vault-dialog').classList.add('open'); }
function vaultDialogClose() { document.getElementById('vault-dialog').classList.remove('open'); document.getElementById('vault-switch-err').style.display = 'none'; }
async function vaultSwitch() {
  const path = document.getElementById('vault-switch-path').value.trim();
  const pass = document.getElementById('vault-switch-pass').value;
  const errEl = document.getElementById('vault-switch-err');
  const btn = document.getElementById('vault-switch-btn');
  if (!path) { errEl.textContent = 'Path is required'; errEl.style.display = 'block'; return; }
  errEl.style.display = 'none';
  if (btn) { btn.disabled = true; btn.textContent = 'Switching…'; }
  try {
    await api('/api/vault/switch', { method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ vault_dir: path, passphrase: pass || undefined }), timeoutMs: 30000 });
    vaultDialogClose(); toast('Switched vault'); loadOverview();
    setVaultPathLabel(path);
  } catch (e) {
    const msg = /timed out/i.test(e.message)
      ? 'Timed out switching vault. The target may be unavailable (USB not connected, drive asleep, or wrong passphrase).'
      : e.message;
    errEl.textContent = msg; errEl.style.display = 'block';
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Switch'; }
  }
}

/* ──────────────── Logout ──────────────── */
async function doLogout() {
  await fetch('/api/logout', { method: 'POST' });
  window.location.href = '/login';
}

/* The dashboard is dark-only; drop the retired light-theme preference. */
try { localStorage.removeItem('elesync-darkmode'); } catch (e) { /* storage blocked */ }

/* ──────────────── Keyboard Shortcuts ──────────────── */
function showShortcutsHelp() {
  // Native <dialog>: focus is trapped, Esc closes it, and focus returns to the opener.
  let dlg = document.getElementById('shortcuts-dialog');
  if (!dlg) {
    const rows = [['N', 'Add a new memory'], ['/', 'Focus search'], ['?', 'Show this help'],
      ['Esc', 'Close a modal or panel'], ['Ctrl/⌘ K', 'Open the command menu']];
    dlg = document.createElement('dialog');
    dlg.id = 'shortcuts-dialog';
    dlg.className = 'dialog shortcuts-dialog';
    dlg.setAttribute('aria-labelledby', 'shortcuts-title');
    dlg.innerHTML = '<h3 id="shortcuts-title">Keyboard shortcuts</h3><dl class="shortcuts-list">' +
      rows.map(([k, label]) => `<dt><kbd>${k}</kbd></dt><dd>${label}</dd>`).join('') +
      '</dl><form method="dialog"><button class="btn" type="submit">Close</button></form>';
    dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
    document.body.appendChild(dlg);
  }
  if (!dlg.open) dlg.showModal();
}
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === '/' && !e.ctrlKey && !e.metaKey) {
    e.preventDefault();
    go('browse');
    document.getElementById('browse-q').focus();
  } else if (e.key.toLowerCase() === 'n' && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
    e.preventDefault();
    go('add');
  } else if (e.key === '?') {
    e.preventDefault();
    showShortcutsHelp();
  } else if (e.key === 'Escape') {
    closeDetail();
    vaultDialogClose();
  }
});

/* ──────────────── Utilities ──────────────── */
function esc(s) { if (!s) return ''; const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function toast(msg, type = 'ok') {
  const el = document.createElement('div'); el.className = `toast ${type}`; el.textContent = msg;
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 3000);
}
