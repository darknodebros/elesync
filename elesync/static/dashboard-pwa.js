/* Reveal the shared artwork only after a complete frame has decoded. */
(() => {
  const elephant = new Image();
  elephant.src = '/static/elesync-ui-elephant-v2.webp';
  elephant.decode().then(() => {
    document.documentElement.classList.add('elephant-ready');
  }).catch(() => { /* Decorative artwork stays hidden if loading fails. */ });
})();

/* ──────────────── Installable app ──────────────── */
let deferredPwaInstallPrompt = null;

function pwaInstallButton() {
  return document.getElementById('pwa-install');
}

function setupPwaInstall() {
  const button = pwaInstallButton();
  if (!button) return;
  const standalone = window.matchMedia?.('(display-mode: standalone)').matches || navigator.standalone;
  if (standalone) { button.hidden = true; return; }
  button.hidden = false;
  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault();
    deferredPwaInstallPrompt = event;
    button.textContent = 'Install app';
  });
  window.addEventListener('appinstalled', () => {
    deferredPwaInstallPrompt = null;
    button.hidden = true;
    toast('EleSync is installed as an app');
  });
}

async function installEleSyncApp() {
  const button = pwaInstallButton();
  if (!deferredPwaInstallPrompt) {
    toast('Use your browser menu to install EleSync or add it to your home screen.');
    return;
  }
  deferredPwaInstallPrompt.prompt();
  const choice = await deferredPwaInstallPrompt.userChoice;
  if (choice.outcome === 'accepted') {
    button.textContent = 'Installing…';
  } else {
    toast('Install was not completed. You can try again whenever you like.');
  }
  deferredPwaInstallPrompt = null;
}
