/* ──────────────── Remote access ──────────────── */
async function loadRemote() {
  const urlEl = document.getElementById('remote-url');
  const hintEl = document.getElementById('remote-hint');
  const certEl = document.getElementById('remote-cert-note');
  const pairingButton = document.getElementById('remote-enable-pairing');
  // Only a real address may be copied; the box also shows status messages.
  urlEl.dataset.address = '';
  try {
    const info = await api('/api/network-info');
    if (!info.auth_enabled) {
      urlEl.textContent = 'Protect dashboard first';
      hintEl.textContent = 'Secure phone pairing requires a dashboard passcode.';
      certEl.textContent = 'Create an owner login before EleSync opens a public HTTPS pairing address.';
      certEl.className = 'remote-cert-note remote-warn';
      certEl.style.display = '';
      pairingButton.hidden = false;
      pairingButton.disabled = false;
      pairingButton.textContent = 'Protect dashboard';
      pairingButton.onclick = () => maybeShowSetup(true);
      updateOverviewCompanion(info, false);
    } else if (info.pairing_url) {
      document.getElementById('remote-pairing-title').textContent = 'Secure connection active';
      document.getElementById('remote-pairing-copy').textContent = 'Keep EleSync running while using your phone.';
      pairingButton.hidden = true;
      pairingButton.disabled = false;
      urlEl.textContent = urlEl.dataset.address = String(info.pairing_url).replace(/\/$/, '');
      await loadPairingQr(info);
      hintEl.textContent = 'Scan this code from your phone. EleSync is managing the secure connection.';
      certEl.textContent = 'Your phone can use this address from any network while the secure pairing session is active.';
      certEl.className = 'remote-cert-note';
      certEl.style.display = '';
      updateOverviewCompanion(info, true);
    } else if (info.local_ip && info.reachable_from_network !== false) {
      const scheme = info.scheme || (info.tls ? 'https' : 'http');
      const url = scheme + '://' + info.local_ip + ':' + info.port;
      urlEl.textContent = urlEl.dataset.address = url;
      await loadPairingQr(info);
      hintEl.textContent = 'Your phone and this computer must be on the same Wi-Fi.';
      certEl.textContent = info.tls
        ? 'Your phone may show a security warning for the local certificate. This is normal on a private network.'
        : 'This local connection is HTTP. Use managed secure pairing for the installable PWA.';
      certEl.style.display = '';
      if (!info.tls) {
        pairingButton.hidden = false;
        pairingButton.disabled = false;
        pairingButton.textContent = 'Use secure pairing';
        pairingButton.onclick = enablePhonePairing;
      } else {
        pairingButton.hidden = true;
      }
      updateOverviewCompanion(info, Boolean(info.tls));
    } else if (info.reachable_from_network === false) {
      urlEl.textContent = 'Secure pairing is not ready yet.';
      hintEl.textContent = 'This EleSync window is local-only, so a phone cannot reach it yet.';
      certEl.textContent = 'Start EleSync with phone pairing enabled, or configure a trusted HTTPS tunnel. The QR is intentionally hidden until the address is reachable.';
      certEl.className = 'remote-cert-note remote-warn';
      certEl.style.display = '';
      document.getElementById('remote-pairing-title').textContent = 'Phone pairing is not enabled';
      document.getElementById('remote-pairing-copy').textContent = 'This desktop session is bound to this computer only.';
      pairingButton.hidden = false;
      pairingButton.disabled = false;
      pairingButton.textContent = 'Enable secure pairing';
      pairingButton.onclick = enablePhonePairing;
      await loadPairingQr(info);
      updateOverviewCompanion(info, false);
    } else {
      urlEl.textContent = 'No phone address is available.';
      hintEl.textContent = 'EleSync could not create a phone-reachable address.';
      certEl.style.display = 'none';
      await loadPairingQr(info);
      updateOverviewCompanion(info, false);
    }
  } catch (e) {
    urlEl.textContent = 'Phone pairing could not be checked.';
    hintEl.textContent = 'Try secure pairing again from this page.';
    certEl.style.display = 'none';
    await loadPairingQr();
    updateOverviewCompanion(null, false);
  } finally {
    pairingButton.style.display = pairingButton.hidden ? 'none' : '';
    const copyButton = document.getElementById('remote-copy-btn');
    if (copyButton) copyButton.disabled = !urlEl.dataset.address;
  }
}

function updateOverviewCompanion(info, pairingReady) {
  const copy = document.getElementById('overview-companion-copy');
  const action = document.getElementById('overview-pairing-action');
  if (!copy || !action) return;
  action.disabled = false;
  if (info && !info.auth_enabled) {
    copy.textContent = 'Protect the dashboard before pairing a phone.';
    action.textContent = 'Protect dashboard →';
    action.onclick = () => maybeShowSetup(true);
    return;
  }
  if (pairingReady) {
    copy.textContent = info?.pairing_url
      ? 'Secure link ready. Scan the code or open the Companion.'
      : 'Ready on your trusted local network.';
    action.textContent = 'Open Companion →';
    action.onclick = () => go('remote');
    return;
  }
  copy.textContent = 'Create a secure phone link. Loopback addresses are never shown as phone destinations.';
  action.textContent = 'Enable pairing →';
  action.onclick = async () => {
    go('remote');
    await enablePhonePairing();
  };
}

async function enablePhonePairing() {
  const button = document.getElementById('remote-enable-pairing');
  if (!button) return;
  button.disabled = true;
  button.textContent = 'Connecting securely…';
  try {
    const result = await api('/api/tunnel/start', {method:'POST', timeoutMs:15000});
    if (!result.success || !result.url) throw new Error(result.error || 'Secure pairing could not start.');
    toast('Secure phone pairing is ready');
    await loadRemote();
  } catch (error) {
    button.disabled = false;
    button.textContent = 'Retry secure pairing';
    document.getElementById('remote-cert-note').textContent = error.message;
    updateOverviewCompanion(null, false);
    toast(error.message, 'err');
  }
}

async function loadPairingQr(info) {
  const targets = [document.getElementById('remote-qr'), document.getElementById('overview-qr')].filter(Boolean);
  if (!targets.length) return;
  try {
    // Only when a phone could reach this dashboard: otherwise the server
    // refuses (409), and every Home visit logged that as an error.
    const network = info || await api('/api/network-info');
    if (!network.reachable_from_network) {
      targets.forEach(target => { target.hidden = true; });
      return false;
    }
    const response = await fetch('/api/network-qr', {cache: 'no-store'});
    if (!response.ok) {
      targets.forEach(target => { target.hidden = true; });
      return false;
    }
    const svg = await response.text();
    const source = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
    targets.forEach(target => { target.src = source; target.hidden = false; });
    return true;
  } catch (_) {
    targets.forEach(target => { target.hidden = true; });
    return false;
  }
}

function copyRemoteUrl() {
  const url = document.getElementById('remote-url').dataset.address;
  if (!url) return;
  navigator.clipboard.writeText(url).then(function() {
    toast('Address copied');
  }).catch(function() {
    toast('Could not copy', 'err');
  });
}

function companionPickFiles() {
  document.getElementById('companion-file-input')?.click();
}

function handleCompanionFiles(files) {
  if (!files?.length) return;
  go('import');
  handleFiles(files);
  toast(`${files.length} file${files.length === 1 ? '' : 's'} ready to import`);
}

function startCompanionVoiceInput() {
  const textarea = document.getElementById('companion-note');
  const button = document.getElementById('companion-voice-btn');
  if (!textarea || !button) return;
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) { toast('Voice input is not available in this browser.', 'err'); return; }
  const recognition = new SpeechRecognition();
  recognition.lang = navigator.language || 'en-US';
  recognition.interimResults = false;
  button.classList.add('recording');
  button.innerHTML = icon('microphone') + ' Listening…';
  recognition.onresult = (event) => { textarea.value = `${textarea.value ? textarea.value + ' ' : ''}${event.results[0][0].transcript}`; };
  recognition.onerror = () => toast('Voice input could not start. You can type the note instead.', 'err');
  recognition.onend = () => { button.classList.remove('recording'); button.innerHTML = icon('microphone') + ' Speak'; };
  recognition.start();
}

async function saveCompanionNote() {
  const textarea = document.getElementById('companion-note');
  const text = textarea?.value.trim();
  if (!text) { toast('Say or type a note first.', 'err'); return; }
  try {
    await api('/api/import', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text, source:'EleSync companion', project_id:activeProjectId() || null})});
    textarea.value = '';
    toast('Note saved');
  } catch (e) { toast(e.message, 'err'); }
}

function toggleCompanionReminder() {
  const input = document.getElementById('companion-reminder-input');
  if (!input) return;
  input.style.display = input.style.display === 'block' ? 'none' : 'block';
  if (input.style.display === 'block') input.focus();
}

/* ──────────────── Import ──────────────── */
function handleDrop(e) { e.preventDefault(); document.getElementById('drop-zone').classList.remove('drag-over'); handleFiles(e.dataTransfer.files); }

async function pasteImport() {
  const text = document.getElementById('paste-area').value.trim();
  if (!text) { toast('Nothing to import. Paste some text first.', 'err'); return; }
  const source = document.getElementById('paste-source').value;
  try {
    const result = await api('/api/import', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text, source, project_id:selectedImportProjectId() || null})});
    document.getElementById('paste-area').value = '';
    document.getElementById('import-result').textContent = `Imported ${result.added || 0} memories.`;
    loadQuickstartProgress();
    toast('Text imported');
  } catch (e) { toast(e.message, 'err'); }
}

let _whatsappReview = null;
const WHATSAPP_SELF_KEY = 'elesync-whatsapp-self';
const WHATSAPP_CHOOSE = '__choose__';
const WHATSAPP_CONSENT = {
  title: 'Save other people\'s messages?',
  body: 'This saves messages written by other people. Only continue if they agreed to it; their messages are personal data. Otherwise, save only your own messages.',
  confirmLabel: 'They agreed, save them',
};

/* WhatsApp exports do not say which sender is the person exporting, so guess it: the name they picked last
   time, the only sender of a one-person chat, or, in a two-person chat exported as "WhatsApp Chat with Ana"
   (or "Chat de WhatsApp con Ana"), the sender who is not Ana. Returns '' when it cannot tell. */
function guessWhatsAppSelf(senders, filename, remembered) {
  if (remembered && senders.includes(remembered)) return remembered;
  if (senders.length === 1) return senders[0];
  if (senders.length !== 2) return '';
  const match = String(filename || '').match(/(?:chat with|whatsapp con|chat -)\s*(.+?)(?:\.(?:txt|zip))?$/i);
  if (!match) return '';
  const other = match[1].trim().toLowerCase();
  const others = senders.filter(sender => sender.trim().toLowerCase() !== other);
  return others.length === 1 ? others[0] : '';
}

function closeWhatsAppReview(result = null) {
  const review = _whatsappReview;
  document.getElementById('whatsapp-review-dialog').classList.remove('open');
  _whatsappReview = null;
  if (review && review.resolve) review.resolve(result);
}

async function filterWhatsAppReview() {
  if (!_whatsappReview) return;
  try { await loadWhatsAppReviewPage(0); }
  catch (error) { toast(error.message, 'err'); }
}

function renderWhatsAppReviewPage(preview) {
  const review = _whatsappReview;
  if (!review) return;
  review.preview = preview;
  review.offset = preview.offset;
  const list = document.getElementById('whatsapp-review-list');
  list.innerHTML = preview.messages.map(message =>
    `<label class="whatsapp-review-row" data-sender="${_gEsc(message.sender)}">` +
    `<input type="checkbox" value="${message.index}" ${review.deselected.has(message.index) ? '' : 'checked'}>` +
    `<span><span class="whatsapp-review-meta">${_gEsc(message.display_time)} · ${_gEsc(message.sender)}</span>${_gEsc(message.text)}</span></label>`
  ).join('');
  list.querySelectorAll('input').forEach(input => input.addEventListener('change', () => {
    const index = Number(input.value);
    if (input.checked) review.deselected.delete(index); else review.deselected.add(index);
  }));
  const pageCount = Math.max(1, Math.ceil(preview.filtered_count / preview.limit));
  const page = Math.floor(preview.offset / preview.limit) + 1;
  document.getElementById('whatsapp-review-page').textContent = `Page ${page} of ${pageCount}`;
  document.getElementById('whatsapp-review-prev').disabled = preview.offset === 0;
  document.getElementById('whatsapp-review-next').disabled = preview.offset + preview.limit >= preview.filtered_count;
}

async function loadWhatsAppReviewPage(offset) {
  if (!_whatsappReview) return;
  const chosen = document.getElementById('whatsapp-sender-filter').value;
  const sender = chosen === WHATSAPP_CHOOSE ? '' : chosen;
  const preview = await api('/api/whatsapp/preview', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({preview_token: _whatsappReview.preview.preview_token, offset, limit: 200, sender}),
  });
  renderWhatsAppReviewPage(preview);
}

function pageWhatsAppReview(change) {
  if (!_whatsappReview) return;
  loadWhatsAppReviewPage(Math.max(0, _whatsappReview.offset + change * _whatsappReview.preview.limit))
    .catch(error => toast(error.message, 'err'));
}

async function reviewWhatsApp(file, text) {
  const preview = await api('/api/whatsapp/preview', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({raw_text: text, filename: file.name}),
  });
  _whatsappReview = {file, preview, offset: 0, deselected: new Set()};
  let remembered = '';
  try { remembered = localStorage.getItem(WHATSAPP_SELF_KEY) || ''; } catch (error) { /* private mode */ }
  const self = guessWhatsAppSelf(preview.senders, file.name, remembered);
  // Only the user's own messages are selected by default; everyone else's needs an explicit choice and consent.
  const senderSelect = document.getElementById('whatsapp-sender-filter');
  senderSelect.innerHTML = (self ? '' : `<option value="${WHATSAPP_CHOOSE}">Choose which sender is you</option>`) +
    preview.senders.map(sender => `<option value="${_gEsc(sender)}">${_gEsc(sender)}${sender === self ? ' (you)' : ''}</option>`).join('') +
    '<option value="">Everyone (needs their consent)</option>';
  senderSelect.value = self || WHATSAPP_CHOOSE;
  document.getElementById('whatsapp-review-summary').textContent =
    `${preview.message_count} message${preview.message_count === 1 ? '' : 's'} · ${preview.senders.length} sender${preview.senders.length === 1 ? '' : 's'}`;
  if (self) await loadWhatsAppReviewPage(0);
  else renderWhatsAppReviewPage(preview);
  document.getElementById('whatsapp-review-err').style.display = 'none';
  document.getElementById('whatsapp-review-dialog').classList.add('open');
  return new Promise(resolve => { _whatsappReview.resolve = resolve; });
}

async function approveWhatsApp(saveFull) {
  if (!_whatsappReview) return;
  const review = _whatsappReview;
  const err = document.getElementById('whatsapp-review-err');
  const selectedSender = document.getElementById('whatsapp-sender-filter').value;
  if (selectedSender === WHATSAPP_CHOOSE && !saveFull) {
    err.textContent = 'Choose which sender is you, so only your own messages are saved.'; err.style.display = 'block';
    return;
  }
  const othersIncluded = saveFull || selectedSender === '' || selectedSender === WHATSAPP_CHOOSE;
  if (othersIncluded && review.preview.senders.length > 1 && !(await confirmDialog(WHATSAPP_CONSENT))) return;
  if (_whatsappReview !== review) return;
  if (selectedSender && selectedSender !== WHATSAPP_CHOOSE) {
    try { localStorage.setItem(WHATSAPP_SELF_KEY, selectedSender); } catch (error) { /* private mode */ }
  }
  const keepOnly = saveFull || selectedSender === WHATSAPP_CHOOSE ? '' : selectedSender;
  const excludedSenders = keepOnly ? review.preview.senders.filter(sender => sender !== keepOnly) : [];
  const redactPhones = document.getElementById('whatsapp-redact-phones').checked;
  const stripLinks = document.getElementById('whatsapp-strip-links').checked;
  const stripMedia = document.getElementById('whatsapp-strip-media').checked;
  try {
    const result = await api('/api/whatsapp/approve', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({preview_token: review.preview.preview_token, select_all: true,
        excluded_indices: Array.from(review.deselected), save_full: saveFull,
        excluded_senders: excludedSenders, redact_phone_numbers: redactPhones, strip_links: stripLinks, strip_media: stripMedia, project_id: selectedImportProjectId() || null}),
    });
    closeWhatsAppReview(result);
  } catch (e) {
    err.textContent = e.message; err.style.display = 'block';
  }
}

async function handleShareTarget() {
  const params = new URLSearchParams(window.location.search);
  const token = params.get('share_target');
  if (token) {
    window.history.replaceState({}, document.title, window.location.pathname);
    try {
      const sharedTarget = await api('/api/share-target?token=' + encodeURIComponent(token));
      const files = (sharedTarget.files || []).map(item => {
        const binary = atob(item.data);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
        return new File([bytes], item.name, {type: item.type || 'application/octet-stream'});
      });
      if (files.length) {
        go('import');
        handleFiles(files);
        toast('Shared file ready to import');
      } else {
        const shared = [sharedTarget.title, sharedTarget.text, sharedTarget.url].filter(Boolean).join('\n');
        const field = document.getElementById('add-content');
        if (field && shared) { field.value = shared; go('add'); toast('Shared content ready to save'); }
      }
    } catch (error) {
      go('import');
      toast('Could not receive the shared file. Use Choose files from phone instead.', 'err');
    }
    return;
  }
  const shared = [params.get('title'), params.get('text'), params.get('url')].filter(Boolean).join('\n');
  if (!shared) return;
  const field = document.getElementById('add-content');
  if (field) { field.value = shared; go('add'); toast('Shared content ready to save'); }
  window.history.replaceState({}, document.title, window.location.pathname);
}

function handleFiles(fileList) {
  // Copy first, then clear the native picker. Browsers otherwise suppress a
  // second change event when the user chooses the same export after a retry.
  const files = Array.from(fileList || []);
  const picker = document.getElementById('file-input');
  if (picker) picker.value = '';
  if (files.length) importFiles(files);
}

let _importPreviewResolve = null;

function resolveImportPreview(shouldImport) {
  const resolve = _importPreviewResolve;
  _importPreviewResolve = null;
  document.getElementById('import-preview-dialog').classList.remove('open');
  if (resolve) resolve(shouldImport);
}

async function showImportPreview(file, preview) {
  document.getElementById('import-preview-title').textContent = `Review ${file.name}`;
  document.getElementById('import-preview-summary').textContent =
    `Detected ${preview.format} CSV · ${preview.sample.length} sample row${preview.sample.length === 1 ? '' : 's'}`;
  document.getElementById('import-preview-columns').textContent =
    'Detected columns: ' + preview.columns.join(', ');
  document.getElementById('import-preview-sample').textContent = preview.sample.length
    ? preview.sample.map(row => Object.entries(row).map(([key, value]) => `${key}: ${value}`).join('\n')).join('\n\n')
    : 'No data rows found. The header is recognized, but there is nothing to import.';
  document.getElementById('import-preview-dialog').classList.add('open');
  return new Promise(resolve => { _importPreviewResolve = resolve; });
}

async function importFiles(fileList) {
  if (_importRunning) return;
  _importRunning = true;
  const queueEl = document.getElementById('import-queue');
  const resultEl = document.getElementById('import-result');
  resultEl.style.display = 'none';
  const files = Array.from(fileList);
  if (!files.length) {
    resultEl.className = 'import-result fail'; resultEl.textContent = 'Choose a file to import.';
    resultEl.style.display = 'block'; _importRunning = false; return;
  }
  let totalAdded = 0, totalSkipped = 0, totalFailed = 0;
  queueEl.innerHTML = '';
  const items = files.map((f, i) => {
    const div = document.createElement('div'); div.className = 'import-item';
    div.innerHTML = `<span class="q-name">${esc(f.name)}</span><div class="q-bar"><div class="q-fill"></div></div><span class="q-status text-muted">queued</span>`;
    queueEl.appendChild(div);
    return { file: f, el: div };
  });
  for (const { file, el } of items) {
    const statusEl = el.querySelector('.q-status');
    const barFill = el.querySelector('.q-fill');
    statusEl.textContent = 'processing…'; statusEl.style.color = 'var(--text)'; barFill.style.width = '50%';
    try {
      const extension = file.name.split('.').pop()?.toLowerCase() || '';
      const text = await file.text();
      const looksLikeWhatsApp = file.name.toLowerCase().includes('whatsapp') ||
        /(?:^|\n)\[?\d{1,4}[/-]\d{1,2}[/-]\d{1,4},?\s+\d{1,2}:\d{2}.*?[-:]\s+[^:]+:/.test(text);
      if (looksLikeWhatsApp) {
        const result = await reviewWhatsApp(file, text);
        if (!result) {
          statusEl.textContent = 'skipped';
          barFill.style.width = '100%';
          continue;
        }
        barFill.style.width = '100%'; statusEl.className = 'q-status done';
        statusEl.textContent = `+${result.added} approved`;
        totalAdded += result.added; totalSkipped += result.skipped_duplicates;
        continue;
      }
      let payload;
      if (extension === 'json') {
        let data;
        try { data = JSON.parse(text); }
        catch (parseErr) { throw new Error(`Not valid JSON: ${parseErr.message}`); }
        payload = { data, source: '', filename: file.name, project_id: selectedImportProjectId() || null };
      } else if (['txt', 'md', 'markdown', 'csv'].includes(extension)) {
        payload = { raw_text: text, source: '', filename: file.name, project_id: selectedImportProjectId() || null };
      } else {
        const bytes = new Uint8Array(await file.arrayBuffer());
        let binary = '';
        for (let offset = 0; offset < bytes.length; offset += 0x8000) binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
        payload = { raw_base64: btoa(binary), source: '', filename: file.name, project_id: selectedImportProjectId() || null };
      }

      barFill.style.width = '80%';
      const result = await api('/api/import', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      barFill.style.width = '100%'; statusEl.className = 'q-status done';
      statusEl.textContent = `+${result.added} (${result.skipped_duplicates} dupes)`;
      totalAdded += result.added; totalSkipped += result.skipped_duplicates;
    } catch (e) {
      barFill.style.width = '100%'; barFill.style.background = 'var(--danger)';
      statusEl.className = 'q-status err';
      statusEl.textContent = e.message.length > 60 ? e.message.slice(0, 60) + '…' : e.message;
      const retry = document.createElement('button');
      retry.className = 'btn btn-sm'; retry.textContent = 'Retry';
      retry.onclick = () => importFiles([file]);
      el.appendChild(retry);
      totalFailed++;
    }
  }
  resultEl.className = totalFailed && !totalAdded ? 'import-result fail' : 'import-result ok';
  const parts = [`Imported ${totalAdded} new memories`];
  if (totalSkipped) parts.push(`${totalSkipped} duplicates skipped`);
  if (totalFailed) parts.push(`${totalFailed} file(s) failed`);
  resultEl.textContent = parts.join(' · ');
  resultEl.style.display = 'block';
  loadOverview();
  _importRunning = false;
  const discovery = document.getElementById('import-discovery');
  if (discovery && totalAdded > 0 && !selectedImportProjectId()) {
    renderProjectDiscovery(discovery, { quietWhenEmpty: true });
  }
}
