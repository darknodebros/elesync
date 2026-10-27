/* ──────────────── Connect AI clients ──────────────── */
async function connectAuto() {
  const btn = document.getElementById('connect-auto-btn');
  const resultEl = document.getElementById('connect-auto-result');
  if (!btn || !resultEl) return;
  btn.disabled = true;
  resultEl.innerHTML = '<div class="empty-sub">Looking for supported AI apps on this computer\u2026</div>';
  try {
    const res = await api('/api/connect/auto', { method:'POST', headers:{'Content-Type':'application/json'}, body:'{}' });
    const wired = res.wired || [];
    if (wired.length === 0) {
      resultEl.innerHTML = '<div class="empty-state compact"><div class="empty-icon">' + icon('magnifying-glass') + '</div>' +
        '<div class="empty-text">No local AI apps found</div>' +
        '<div class="empty-sub">No supported local app was found. Choose an AI collaborator below to continue with its guided connection.</div></div>';
    } else {
      const items = wired.map(w => {
        if (w.error) {
          return `<li><strong>${_gEsc(w.client)}</strong> \u2014 couldn\u2019t write config: ${_gEsc(w.error)}</li>`;
        }
        const status = w.already_wired ? 'was already connected' : 'connected';
        return `<li><strong>${_gEsc(w.client)}</strong> \u2014 ${status}. Restart the app to activate.</li>`;
      }).join('');
      resultEl.innerHTML = `<div class="connect-output"><div class="connect-output-title">${wired.length} app${wired.length > 1 ? 's' : ''} connected</div>` +
        `<ul class="connect-steps bullet-list">${items}</ul></div>`;
      toast(`Connected ${wired.length} app${wired.length > 1 ? 's' : ''}`);
    }
  } catch (e) {
    resultEl.innerHTML = '<div class="empty-state compact"><div class="empty-text text-danger">' + _gEsc(e.message) + '</div></div>';
  } finally {
    btn.disabled = false;
  }
}

const READINESS_LIVE_MINUTES = 30;

function minutesSince(timestamp) {
  const time = new Date(timestamp || '').getTime();
  return Number.isNaN(time) ? Infinity : (Date.now() - time) / 60000;
}

function readinessAssistants(data) {
  const activity = data.activity || {};
  const describeSeen = seen => `${timeAgo(seen.last_seen) || 'just'} ago`;
  const rows = [...(data.local_clients || []), ...(data.remote_clients || [])].map(client => {
    const seen = activity[client.ai];
    const configured = client.status === 'connected';
    const minutes = seen ? minutesSince(seen.last_seen) : Infinity;
    let state = 'off';
    let detail = client.detail || '';
    if (minutes <= READINESS_LIVE_MINUTES) { state = 'live'; detail = `Reached EleSync ${describeSeen(seen)}`; }
    else if (seen) { state = 'waiting'; detail = `${configured ? 'Connected · ' : ''}last reached EleSync ${describeSeen(seen)}`; }
    else if (configured) { state = 'waiting'; detail = 'Connected · not heard from yet'; }
    return { ...client, state, detail, configured };
  });
  Object.entries(activity).forEach(([ai, seen]) => {
    if (!ai || ai === 'phone' || rows.some(row => row.ai === ai)) return;
    const live = minutesSince(seen.last_seen) <= READINESS_LIVE_MINUTES;
    rows.push({ ai, label: relayLabel(ai), kind: 'seen', configured: true, state: live ? 'live' : 'waiting', detail: `${live ? 'Reached' : 'Last reached'} EleSync ${describeSeen(seen)}` });
  });
  const unnamed = activity[''];
  if (unnamed && minutesSince(unnamed.last_seen) <= READINESS_LIVE_MINUTES) {
    rows.push({ ai: '', label: 'Unnamed assistant', kind: 'seen', configured: false, state: 'live', detail: `Reached EleSync ${describeSeen(unnamed)} without saying which app it is` });
  }
  const order = { live: 0, waiting: 1, off: 2 };
  return rows.sort((a, b) => order[a.state] - order[b.state] || a.label.localeCompare(b.label));
}

function readinessRow(row) {
  const dot = row.state === 'live' || row.state === 'ok' ? 'connected' : row.state === 'waiting' || row.state === 'warn' ? 'ready' : '';
  const label = { live: 'live', waiting: 'waiting', off: 'not set up', ok: 'ok', warn: 'check' }[row.state] || row.state;
  const action = row.kind === 'security' && row.state !== 'ok'
    ? '<button class="btn btn-sm btn-primary" data-readiness-security>Protect dashboard</button>'
    : row.kind === 'backup' && row.state !== 'ok'
      ? '<button class="btn btn-sm btn-primary" data-readiness-backup>Back up now</button>'
      : row.kind === 'local' && row.state === 'off'
      ? '<button class="btn btn-sm" onclick="connectAuto()">Find app</button>'
      : row.kind === 'remote' && row.state === 'off'
        ? `<button class="btn btn-sm" data-readiness-connect="${_gEsc(row.id)}">Set up</button>`
        : '';
  return `<div class="readiness-row"><span class="readiness-status ${dot}"></span><div class="readiness-label"><strong>${_gEsc(row.label)}</strong><small title="${_gEsc(row.detail || '')}">${_gEsc(row.detail || '')}</small></div><span class="readiness-state">${_gEsc(label)}</span>${action}</div>`;
}

async function loadReadiness() {
  const summary = document.getElementById('readiness-summary');
  const list = document.getElementById('readiness-list');
  if (!summary || !list) return;
  try {
    const data = await api('/api/readiness');
    const assistants = readinessAssistants(data);
    const live = assistants.filter(a => a.state === 'live');
    const waiting = assistants.filter(a => a.configured && a.state !== 'live');
    const ready = data.vault.ready && live.length > 0 && waiting.length === 0;
    const names = rows => rows.map(r => r.label).join(', ').replace(/, ([^,]*)$/, ' and $1');
    const headline = ready
      ? `Ready: ${names(live)} reached EleSync in the last ${READINESS_LIVE_MINUTES} minutes`
      : !live.length
        ? `Not ready yet. No assistant has reached EleSync in the last ${READINESS_LIVE_MINUTES} minutes`
        : `Almost ready, waiting on ${names(waiting)}`;
    summary.innerHTML = `<span class="readiness-dot ${ready ? 'ok' : 'warn'}"></span>`
      + `<strong>${_gEsc(headline)}</strong>`
      + `<span>EleSync ${_gEsc(data.version)}</span>`
      + (ready ? '' : '<p class="readiness-hint">Ask each assistant: “Search EleSync for anything about me.” Its row turns green as soon as it reaches your vault.</p>');

    const backupAt = data.backup && data.backup.last_backup_at;
    const tunnelUrl = data.tunnel && data.tunnel.url ? ' · ' + data.tunnel.url.replace(/^https?:\/\//, '') : '';
    const phoneSeen = (data.activity || {}).phone;
    const tunnelOn = Boolean(data.tunnel && data.tunnel.active);
    const phoneLive = phoneSeen && minutesSince(phoneSeen.last_seen) <= READINESS_LIVE_MINUTES;
    const phoneNote = phoneSeen ? ` · last note from your phone ${timeAgo(phoneSeen.last_seen) || 'just'} ago` : '';
    const phoneDetail = (tunnelOn ? `Public link active${tunnelUrl}` : 'Same Wi-Fi only · start a public link to use other networks') + phoneNote;
    const setup = [
      { label: 'Vault', state: data.vault.ready ? 'ok' : 'warn', detail: data.vault.path },
      { label: 'Dashboard security', kind: 'security', state: data.security.auth ? 'ok' : 'warn', detail: data.security.auth ? 'Username and password enabled' : 'Add a user before network access' },
      { label: 'Phone', state: tunnelOn || phoneLive ? 'ok' : 'off', detail: phoneDetail },
      { label: 'Backup', kind: 'backup', state: backupAt && minutesSince(backupAt) < 7 * 24 * 60 ? 'ok' : 'warn', detail: backupAt ? `Last backup ${timeAgo(backupAt) || 'just'} ago` : 'No backup yet' },
    ];
    list.innerHTML = '<div class="readiness-group">Assistants</div>' + assistants.map(readinessRow).join('')
      + '<div class="readiness-group">Setup</div>' + setup.map(readinessRow).join('');
    list.querySelectorAll('[data-readiness-connect]').forEach(button => {
      button.addEventListener('click', () => connectSelect(button.dataset.readinessConnect));
    });
    list.querySelector('[data-readiness-security]')?.addEventListener('click', () => maybeShowSetup(true));
    list.querySelector('[data-readiness-backup]')?.addEventListener('click', event => backupNow(event.currentTarget));
  } catch (error) {
    summary.textContent = 'Connection health is temporarily unavailable.';
  }
}

function loadConnect() {
  loadReadiness();
  loadApiKeys();
  const grid = document.getElementById('connect-grid');
  if (!grid) return;
  grid.innerHTML = CONNECT_CLIENTS.map(c =>
    `<button type="button" class="connect-card" data-client="${_gEsc(c.id)}" style="--client-color:${c.color}" aria-label="Select ${c.label}">` +
      `<span class="connect-name">${_gEsc(c.label)}</span>` +
    `<span class="connect-kind">${c.kind === 'remote' ? 'Remote connector' : c.kind === 'gemini' ? 'Google account' : 'Local app'}</span>` +
      `<span class="connect-desc">${_gEsc(c.desc)}</span>` +
    `</button>`
  ).join('');
  grid.querySelectorAll('[data-client]').forEach(btn => {
    btn.addEventListener('click', () => connectSelect(btn.dataset.client));
  });
}

function connectClientMeta(id) {
  return CONNECT_CLIENTS.find(c => c.id === id) || CONNECT_CLIENTS[0];
}

function connectPanelHtml(client) {
  const c = connectClientMeta(client);
  if (c.kind === 'gemini') {
    return `<div class="connect-selected"><div><div class="connect-heading">Gemini</div>` +
      `<div class="empty-sub">Use your own Google Gemini account. EleSync never asks for your Google password.</div></div></div>` +
      `<div class="tunnel-card"><div class="card-title">Connect with a Gemini API key</div>` +
      `<div class="setup-hint mb-10">Create a key in <a href="https://aistudio.google.com/app/apikey" target="_blank" rel="noopener">Google AI Studio</a>, then paste it here. EleSync will verify it with Google before saving it locally.</div>` +
      `<label class="add-label" for="gemini-api-key">Gemini API key</label><input class="search-input w-full" id="gemini-api-key" type="password" autocomplete="off" placeholder="Paste your Gemini API key">` +
      `<div class="row-center flex-wrap mt-12"><button class="btn btn-primary" id="connect-gemini-btn">Verify and connect</button><span id="gemini-status" class="setup-hint"></span></div></div>` +
      `<div id="connect-error" class="connect-error" style="display:none"></div><div class="mt-14" id="connect-result"></div>`;
  }
  if (c.kind === 'local') {
    return `<div class="connect-selected"><div><div class="connect-heading">${_gEsc(c.label)}</div>` +
      `<div class="empty-sub">This client runs on your computer, so EleSync can wire it through local MCP config.</div></div></div>` +
      `<button class="btn btn-primary" id="connect-generate-btn" data-connect-generate="${_gEsc(c.id)}">Connect this app</button>` +
      `<div id="connect-error" class="connect-error" style="display:none"></div>` +
      `<div class="mt-14" id="connect-result"></div>`;
  }
  return `<div class="connect-selected"><div><div class="connect-heading">${_gEsc(c.label)}</div>` +
    `<div class="empty-sub">Use this when ${_gEsc(c.label)} supports adding a custom MCP connector URL.</div></div></div>` +
    `<div class="tunnel-card" id="tunnel-card">` +
      `<div class="card-title">Prepare secure connection</div>` +
      `<div class="row-center gap-10 mb-12">` +
        `<button class="btn btn-primary" id="tunnel-start-btn" onclick="tunnelStart()">Create secure link</button>` +
        `<div id="tunnel-status-text" class="setup-hint">EleSync will create and manage the secure connection for you.</div>` +
      `</div>` +
    `</div>` +
    `<details class="add-optional"><summary>Other connection options</summary><div class="mt-12">` +
    `<div class="grid-2">` +
      `<div><label class="add-label" for="connect-url">Public HTTPS URL</label><input class="search-input w-full" id="connect-url" placeholder="https://your-domain.example/mcp" oninput="connectValidate()"></div>` +
      `<div><label class="add-label" for="connect-host">Tunnel/domain hostname</label><input class="search-input w-full" id="connect-host" placeholder="xxxx.lhr.life" oninput="connectValidate()"></div>` +
    `</div></div></details>` +
    `<div class="row-center flex-wrap mt-12">` +
      `<label class="setup-choice m-0"><input type="checkbox" id="connect-force-token" onchange="connectValidate()"> Replace token</label>` +
      `<label class="setup-choice m-0"><input type="checkbox" id="connect-insecure" onchange="connectValidate()"> Local HTTP test only</label>` +
      `<button class="btn btn-primary" id="connect-generate-btn" data-connect-generate="${_gEsc(c.id)}">Generate setup</button>` +
    `</div>` +
    `<div id="connect-error" class="connect-error" style="display:none"></div>` +
    `<div class="mt-14" id="connect-result"></div>`;
}

function connectSelect(client) {
  document.querySelectorAll('.connect-card').forEach(el => el.classList.toggle('selected', el.dataset.client === client));
  const panel = document.getElementById('connect-panel');
  panel.innerHTML = `<div class="card-title">Setup details</div>${connectPanelHtml(client)}`;
  panel.querySelectorAll('[data-connect-generate]').forEach(btn => {
    btn.addEventListener('click', () => connectGenerate(btn.dataset.connectGenerate || client));
  });
  connectValidate();
  if (client === 'gemini') {
    document.getElementById('connect-gemini-btn')?.addEventListener('click', connectGemini);
    document.getElementById('gemini-api-key')?.focus();
  } else {
    document.getElementById('connect-url')?.focus();
  }
}

function connectValidate() {
  const c = connectClientMeta(document.querySelector('.connect-card.selected')?.dataset.client || '');
  const btn = document.getElementById('connect-generate-btn');
  const errEl = document.getElementById('connect-error');
  if (!btn || !c.id) return;

  if (c.kind === 'local') {
    btn.disabled = false;
    errEl.style.display = 'none';
    return true;
  }
  if (c.kind === 'gemini') return true;

  const url = (document.getElementById('connect-url')?.value || '').trim();
  const host = (document.getElementById('connect-host')?.value || '').trim();
  const insecure = document.getElementById('connect-insecure')?.checked || false;

  let err = '';
  if (!url && !host) {
    err = 'Enter a public HTTPS URL or a tunnel/domain hostname so EleSync knows which host to allow.';
  } else if (url && !url.startsWith('https://') && !(insecure && url.startsWith('http://'))) {
    err = 'The public URL must start with https:// (or enable “Local HTTP test only” for http://).'; }

  if (err) {
    btn.disabled = true;
    errEl.textContent = err;
    errEl.style.display = 'block';
    return false;
  }
  btn.disabled = false;
  errEl.style.display = 'none';
  return true;
}

async function connectGemini() {
  const keyInput = document.getElementById('gemini-api-key');
  const status = document.getElementById('gemini-status');
  const result = document.getElementById('connect-result');
  const error = document.getElementById('connect-error');
  const button = document.getElementById('connect-gemini-btn');
  if (!keyInput || !button) return;
  const apiKey = keyInput.value.trim();
  if (!apiKey) { error.textContent = 'Paste your Gemini API key first.'; error.style.display = 'block'; return; }
  button.disabled = true;
  status.textContent = 'Verifying with Google…';
  error.style.display = 'none';
  try {
    const data = await api('/api/connect/gemini', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({api_key:apiKey})});
    keyInput.value = '';
    status.textContent = 'Connected and verified';
    status.style.color = 'var(--success)';
    result.innerHTML = `<div class="connect-output"><div class="connect-output-title">Gemini is connected</div><div class="empty-sub">EleSync verified a real Gemini response using ${_gEsc(data.model)}. Your key stays in this EleSync installation.</div></div>`;
    loadReadiness();
  } catch (e) {
    status.textContent = '';
    error.textContent = e.message;
    error.style.display = 'block';
  } finally { button.disabled = false; }
}

async function tunnelStart() {
  const btn = document.getElementById('tunnel-start-btn');
  const txt = document.getElementById('tunnel-status-text');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Starting…';
  try {
    const res = await api('/api/tunnel/start', { method: 'POST', timeoutMs: 15000 });
    if (res.url) {
      document.getElementById('connect-url').value = res.url + '/mcp';
      connectValidate();
      const selected = document.querySelector('.connect-card.selected')?.dataset.client;
      if (selected) await connectGenerate(selected);
    }
    renderTunnelStatus(res);
  } catch (e) {
    txt.textContent = e.message;
    txt.style.color = 'var(--danger)';
    btn.disabled = false;
    btn.textContent = 'Connect securely';
  }
}

async function tunnelStop() {
  const btn = document.getElementById('tunnel-start-btn');
  if (!btn) return;
  btn.disabled = true;
  try {
    await api('/api/tunnel/stop', { method: 'POST' });
    tunnelRefresh();
  } catch (e) {
    toast(e.message, 'err');
    btn.disabled = false;
  }
}

async function tunnelRefresh() {
  try {
    const res = await api('/api/tunnel/status');
    renderTunnelStatus(res);
  } catch (e) {}
}

function renderTunnelStatus(res) {
  const btn = document.getElementById('tunnel-start-btn');
  const txt = document.getElementById('tunnel-status-text');
  if (!btn || !txt) return;

  if (res.active && res.url) {
    txt.innerHTML = `Tunnel active: <code class="text-success">${_gEsc(res.url)}</code>`;
    btn.textContent = 'Stop tunnel';
    btn.className = 'btn btn-danger';
    btn.onclick = tunnelStop;
    btn.disabled = false;
  } else if (res.error) {
    txt.textContent = res.error;
    txt.style.color = 'var(--danger)';
    btn.textContent = 'Retry tunnel';
    btn.className = 'btn btn-primary';
    btn.onclick = tunnelStart;
    btn.disabled = false;
  } else {
    txt.textContent = 'EleSync will create and manage the secure connection for you.';
    txt.style.color = '';
    btn.textContent = 'Connect securely';
    btn.className = 'btn btn-primary';
    btn.onclick = tunnelStart;
    btn.disabled = false;
  }
}

async function connectGenerate(client) {
  if (!connectValidate()) return;
  const c = connectClientMeta(client);
  const resultEl = document.getElementById('connect-result');
  resultEl.innerHTML = skel([92, 80, 88]);
  const body = { client };
  if (c.kind === 'remote') {
    body.url = document.getElementById('connect-url').value;
    body.allowed_host = document.getElementById('connect-host').value;
    body.force_token = document.getElementById('connect-force-token').checked;
    body.insecure = document.getElementById('connect-insecure').checked;
  }
  try {
    const plan = await api('/api/connect', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
    resultEl.innerHTML = renderConnectPlan(plan);
    bindConnectCopyButtons(resultEl);
    resultEl.querySelector('[data-verify-connection]')?.addEventListener('click', testConnectPlan);
  } catch (e) {
    resultEl.innerHTML = '<div class="empty-state compact"><div class="empty-text text-danger">' + _gEsc(e.message) + '</div></div>';
  }
}

function renderConnectPlan(plan) {
  if (plan.kind === 'local') {
    return `<div class="connect-output"><div class="connect-output-title">${_gEsc(plan.label)} local setup</div>` +
      `<div class="empty-sub">EleSync prepared the local connection. Restart ${_gEsc(plan.label)} to finish.</div>` +
      `<details class="technical-details"><summary>Show technical details</summary><div class="technical-details-body">` +
      connectCopyRow('Command', plan.command) +
      `<ol class="connect-steps">${(plan.steps||[]).map(s => `<li>${_gEsc(s)}</li>`).join('')}</ol></div></details></div>`;
  }
  return `<div class="connect-output"><div class="connect-output-title">Finish ${_gEsc(plan.label)} connection</div>` +
    `<div class="empty-sub">EleSync prepared the secure connection details for ${_gEsc(plan.label)}.</div>` +
    `<button class="btn btn-primary btn-sm" data-verify-connection="1" data-verify-url="${_gEsc(plan.server_url)}" data-verify-token="${_gEsc(plan.token)}">Check connection</button><div class="connect-verify-result mt-8"></div>` +
    `<details class="technical-details"><summary>Show connection details</summary><div class="technical-details-body">` +
    connectCopyRow('Server URL', plan.server_url) +
    connectCopyRow('Authorization header', 'Authorization: Bearer ' + plan.token) +
    connectCopyRow('Local serve command', plan.serve_command) +
    connectCopyRow('Tunnel target', plan.local_target) +
    `<ol class="connect-steps">${(plan.steps||[]).map(s => `<li>${_gEsc(s)}</li>`).join('')}</ol></div></details></div>`;
}

async function testConnectPlan(event) {
  const button = event.currentTarget;
  const result = button.parentElement.querySelector('.connect-verify-result');
  button.disabled = true;
  result.textContent = 'Testing the MCP endpoint…';
  try {
    const data = await api('/api/connect/verify', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url:button.dataset.verifyUrl, token:button.dataset.verifyToken})});
    result.textContent = data.detail;
    result.style.color = data.verified ? 'var(--success)' : 'var(--warn)';
    if (data.verified) localStorage.setItem('elesync-connection-verified', '1');
    loadQuickstartProgress();
  } catch (e) {
    result.textContent = e.message;
    result.style.color = 'var(--danger)';
  } finally {
    button.disabled = false;
  }
}

/* ──────────────── API keys (public REST API, see docs/API.md) ──────────────── */
let _apiKeyProjects = [];

async function loadApiKeys() {
  const list = document.getElementById('api-key-list');
  if (!list) return;
  const base = document.getElementById('api-base-url');
  if (base) base.textContent = `${location.origin}/api/v1`;
  try {
    const [keys, projects] = await Promise.all([api('/api/api-keys'), api('/api/projects')]);
    _apiKeyProjects = projects.projects || [];
    const select = document.getElementById('api-key-project');
    if (select) {
      const chosen = select.value;
      select.innerHTML = '<option value="">Whole vault</option>' +
        _apiKeyProjects.map(p => `<option value="${_gEsc(p.id)}">${_gEsc(p.name)}</option>`).join('');
      select.value = _apiKeyProjects.some(p => p.id === chosen) ? chosen : '';
    }
    renderApiKeys(keys.keys || []);
  } catch (error) {
    list.innerHTML = `<div class="empty-sub">${_gEsc(error.message)}</div>`;
  }
}

function renderApiKeys(keys) {
  const list = document.getElementById('api-key-list');
  if (!keys.length) {
    list.innerHTML = '<div class="empty-sub">No keys yet.</div>';
    return;
  }
  const projectName = id => (_apiKeyProjects.find(p => p.id === id) || {}).name || 'One project';
  list.innerHTML = keys.map(key => {
    const scope = key.project_id ? projectName(key.project_id) : 'Whole vault';
    const access = key.access === 'write' ? 'Read and save' : 'Read only';
    const used = key.last_used_at ? `used ${timeAgo(key.last_used_at) || 'just now'} ago` : 'never used';
    return `<div class="api-key-row"><div class="api-key-label"><strong>${_gEsc(key.name)}</strong>` +
      `<small>${_gEsc(access)} · ${_gEsc(scope)} · <code>${_gEsc(key.preview)}</code> · ${_gEsc(used)}</small></div>` +
      `<button class="btn btn-sm btn-danger write-only" data-revoke-key="${_gEsc(key.id)}" data-key-name="${_gEsc(key.name)}">Revoke</button></div>`;
  }).join('');
  list.querySelectorAll('[data-revoke-key]').forEach(button => {
    button.addEventListener('click', () => revokeApiKey(button.dataset.revokeKey, button.dataset.keyName));
  });
}

async function createApiKey() {
  const err = document.getElementById('api-key-error');
  const created = document.getElementById('api-key-created');
  const nameInput = document.getElementById('api-key-name');
  const button = document.getElementById('api-key-create-btn');
  err.style.display = 'none';
  const body = {
    name: nameInput.value.trim(),
    access: document.getElementById('api-key-access').value,
    project_id: document.getElementById('api-key-project').value || null,
  };
  if (!body.name) {
    err.textContent = 'Give the key a name, for example the app that will use it.';
    err.style.display = 'block';
    nameInput.focus();
    return;
  }
  button.disabled = true;
  try {
    const result = await api('/api/api-keys', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    created.innerHTML = '<div class="connect-output"><div class="connect-output-title">Key created</div>' +
      '<div class="empty-sub">Copy it now and paste it into the app. EleSync will not show it again.</div>' +
      connectCopyRow(result.key.name, result.secret) + '</div>';
    bindConnectCopyButtons(created);
    nameInput.value = '';
    loadApiKeys();
  } catch (error) {
    err.textContent = error.message;
    err.style.display = 'block';
  } finally {
    button.disabled = false;
  }
}

async function revokeApiKey(id, name) {
  const confirmed = await confirmDialog({
    title: 'Revoke this key?',
    body: `Anything using "${name}" loses access to EleSync right away. This cannot be undone.`,
    confirmLabel: 'Revoke key',
    danger: true,
  });
  if (!confirmed) return;
  try {
    await api('/api/api-keys/' + encodeURIComponent(id), {method:'DELETE'});
    document.getElementById('api-key-created').innerHTML = '';
    toast('Key revoked');
    loadApiKeys();
  } catch (error) {
    toast(error.message, 'err');
  }
}

function connectCopyRow(label, value) {
  const id = 'copy-' + Math.random().toString(36).slice(2);
  return `<div class="copy-row"><div><div class="copy-label">${_gEsc(label)}</div><code id="${id}">${_gEsc(value)}</code></div>` +
    `<button class="btn btn-sm" data-copy-target="${_gEsc(id)}">Copy</button></div>`;
}

function bindConnectCopyButtons(root) {
  (root || document).querySelectorAll('[data-copy-target]').forEach(btn => {
    btn.addEventListener('click', () => copyText(btn.dataset.copyTarget));
  });
}

async function copyText(id) {
  const txt = document.getElementById(id).textContent;
  try {
    await navigator.clipboard.writeText(txt);
    toast('Copied');
  } catch (_) {
    toast('Copy failed - select the text manually', 'err');
  }
}
