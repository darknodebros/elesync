/* Guided setup and first-run recovery states. */
const GUIDED_STEPS = [
  ['Create your workspace', 'Choose where EleSync keeps your private context. Security and access settings are handled here when needed.', 'setup'],
  ['Add your context', 'Import your AI history, notes, or conversations, then give them a shared home in a project.', 'import'],
  ['Connect an AI', 'Choose an assistant, give it the project access it needs, and verify the connection.', 'connect'],
];
let guidedIndex = 0;

function startGuidedSetup() {
  guidedIndex = 0;
  document.getElementById('guided-dialog')?.classList.add('open');
  renderGuidedStep();
}
function renderGuidedStep() {
  const step = GUIDED_STEPS[guidedIndex];
  document.getElementById('guided-step-label').textContent = `STEP ${guidedIndex + 1} OF ${GUIDED_STEPS.length}`;
  document.getElementById('guided-title').textContent = step[0];
  document.getElementById('guided-body').textContent = step[1];
  document.getElementById('guided-progress').textContent = `Step ${guidedIndex + 1} of ${GUIDED_STEPS.length}`;
  document.getElementById('guided-back-btn').disabled = guidedIndex === 0;
  document.getElementById('guided-next-btn').textContent = guidedIndex === GUIDED_STEPS.length - 1 ? 'Done' : 'Next';
}
function guidedAction() {
  document.getElementById('guided-dialog')?.classList.remove('open');
  openQuickstart(GUIDED_STEPS[guidedIndex][2]);
}
function guidedBack() {
  if (guidedIndex > 0) { guidedIndex -= 1; renderGuidedStep(); }
}
function guidedNext() {
  if (guidedIndex >= GUIDED_STEPS.length - 1) {
    document.getElementById('guided-dialog')?.classList.remove('open');
    return;
  }
  guidedIndex += 1;
  renderGuidedStep();
}

function showQuickstartIfNeeded() {
  if (localStorage.getItem('elesync-hide-quickstart') === '1') {
    document.getElementById('quickstart-card')?.remove();
  }
}

async function loadQuickstartProgress() {
  const summary = document.getElementById('quickstart-progress');
  if (!summary) return;
  try {
    const p = await api('/api/onboarding/progress');
    const steps = [
      Boolean(p.vault && p.secured && p.project),
      Boolean(p.imported && p.project),
      Boolean(p.connected && (p.verified || localStorage.getItem('elesync-connection-verified') === '1')),
    ];
    const done = steps.filter(Boolean).length;
    summary.textContent = `${done} of 3 steps complete`;
  } catch (e) {
    summary.textContent = 'Start with the guide below.';
  }
}

async function openQuickstart(page) {
  if (page === 'setup') {
    try {
      await maybeShowSetup(true);
    } catch (e) {
      go('connect');
    }
    return;
  }
  go(page);
}

function setupShowError(message) {
  const errEl = document.getElementById('setup-err');
  if (!errEl) return;
  errEl.textContent = message;
  errEl.style.display = 'block';
}

async function maybeShowSetup(force = false) {
  try {
    const setup = await api('/api/setup');
    if (!setup.first_run && !force) return;
    const protect = document.getElementById('setup-protect');
    const vaultPath = document.getElementById('setup-vault-path');
    if (vaultPath) vaultPath.value = setup.vault_dir || '';
    protect.checked = false;
    protect.onchange = setupPassToggle;
    document.getElementById('setup-usb').checked = false;
    setupPassToggle();
    document.getElementById('setup-dialog').classList.add('open');
  } catch (e) {
    setupShowError('Setup is temporarily unavailable. Check that EleSync is running, then retry.');
    document.getElementById('setup-dialog')?.classList.add('open');
  }
}

function setupPassToggle() {
  const show = document.getElementById('setup-protect').checked;
  document.getElementById('setup-pass-fields').style.display = show ? '' : 'none';
}

async function setupSubmit(skip) {
  const errEl = document.getElementById('setup-err');
  const finishBtn = document.getElementById('setup-finish-btn');
  const skipBtn = document.getElementById('setup-skip-btn');
  const btn = skip ? skipBtn : finishBtn;
  const protect = document.getElementById('setup-protect').checked && !skip;
  const usbNext = document.getElementById('setup-usb').checked && !skip;
  const username = document.getElementById('setup-username')?.value.trim() || 'owner';
  const pass = document.getElementById('setup-pass').value;
  const pass2 = document.getElementById('setup-pass2').value;
  const vaultDir = document.getElementById('setup-vault-path')?.value.trim();
  if (protect && (!username || /\s/.test(username))) { setupShowError('Enter a username without spaces.'); return; }
  if (protect && !pass) { setupShowError('Enter a passcode, or uncheck passcode lock.'); return; }
  if (protect && pass.length < 8) { setupShowError('Passcode must be at least 8 characters.'); return; }
  if (protect && pass !== pass2) { setupShowError('Passcodes do not match.'); return; }
  errEl.style.display = 'none';
  [finishBtn, skipBtn].forEach(button => { if (button) button.disabled = true; });
  if (btn) btn.textContent = skip ? 'Skipping...' : 'Saving...';
  let failed = false;
  try {
    await api('/api/setup/complete', { method:'POST', timeoutMs:15000, headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ vault_dir: vaultDir, username, protect_dashboard: protect, passcode: protect ? pass : '' }) });
    document.getElementById('setup-dialog').classList.remove('open');
    document.getElementById('setup-username').value = '';
    document.getElementById('setup-pass').value = '';
    document.getElementById('setup-pass2').value = '';
    toast(protect ? 'Passcode lock is on' : 'Setup saved');
    if (usbNext) { go('usb'); usbRefresh(); }
  } catch (e) {
    failed = true;
    setupShowError(e.message || 'Setup could not be saved. Your choices are still here; retry when ready.');
  } finally {
    if (finishBtn) { finishBtn.disabled = false; finishBtn.textContent = failed ? 'Retry setup' : 'Finish setup'; }
    if (skipBtn) { skipBtn.disabled = false; skipBtn.textContent = 'Skip'; }
  }
}
function setupFinish() { setupSubmit(false); }
function setupSkip() { setupSubmit(true); }
