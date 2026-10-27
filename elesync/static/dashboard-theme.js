/* Theme switch: "cosmic" (space black, gold, glass; the default) or "classic" (violet).
   The page HTML starts as <html data-theme="cosmic">; this script, loaded first in the head, removes it before
   first paint when the user has picked Classic, so neither theme flashes. The sign-in page loads this too.
   Only an explicit pick is stored. Up to 1.24.70 every page load stored the current theme under
   'elesync-theme', so that key cannot tell a real choice from the old default and is dropped. */
const THEME_KEY = 'elesync-theme-choice';
const LEGACY_THEME_KEY = 'elesync-theme';
const THEME_COLORS = { classic: '#0c0c14', cosmic: '#05060b' };
// Images that cannot take a CSS filter (or turn red and green under a hue shift) have gold copies,
// made by scripts/build_cosmic_icons.py: [selector, attribute, classic file, cosmic file].
const THEME_IMAGES = [
  ['link[rel="icon"]', 'href', '/static/elesync-favicon.png', '/static/elesync-favicon-cosmic.png'],
  ['link[rel="apple-touch-icon"]', 'href', '/static/elesync-main-logo.png', '/static/elesync-main-logo-cosmic.png'],
  ['img.sidebar-brand-lockup', 'src', '/static/elesync-main-logo.webp', '/static/elesync-main-logo-cosmic.webp'],
];

function currentTheme() { return document.documentElement.dataset.theme === 'cosmic' ? 'cosmic' : 'classic'; }
function applyTheme(name) {
  if (name === 'classic') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = 'cosmic';
}
function applyThemeImages() {
  const cosmic = currentTheme() === 'cosmic';
  for (const [selector, attribute, classic, gold] of THEME_IMAGES) {
    document.querySelectorAll(selector).forEach(element => element.setAttribute(attribute, cosmic ? gold : classic));
  }
}
function syncThemeChrome() {
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', THEME_COLORS[currentTheme()]);
  applyThemeImages();
  const button = document.getElementById('theme-switch');
  if (button) button.textContent = currentTheme() === 'cosmic' ? 'Classic theme' : 'Cosmic theme';
  // The graphs paint their own backdrop, so they redraw in the new colours.
  if (typeof graphScene !== 'undefined' && graphScene) graphScene.restyle();
  if (typeof homeGraphScene !== 'undefined' && homeGraphScene) homeGraphScene.restyle();
}
function setTheme(name) {
  applyTheme(name);
  try { localStorage.setItem(THEME_KEY, currentTheme()); } catch (error) { /* private mode: the choice just will not persist */ }
  syncThemeChrome();
}
function toggleTheme() { setTheme(currentTheme() === 'cosmic' ? 'classic' : 'cosmic'); }
// Applied as soon as this script loads from the head, before first paint.
try {
  localStorage.removeItem(LEGACY_THEME_KEY);
  applyTheme(localStorage.getItem(THEME_KEY) === 'classic' ? 'classic' : 'cosmic');
} catch (error) { applyTheme('cosmic'); /* private mode: the default */ }
document.addEventListener('DOMContentLoaded', syncThemeChrome);
