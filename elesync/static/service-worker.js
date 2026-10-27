// Bump this whenever the application shell changes so installed PWAs discard
// stale dashboard CSS/JS instead of continuing to render an older interface.
const CACHE = 'elesync-pwa-v70';
const SHELL = [
  '/',
  '/static/dashboard.css',
  '/static/dashboard-command-center.css',
  '/static/dashboard-api.js',
  '/static/dashboard-onboarding.js',
  '/static/dashboard-projects.js',
  '/static/dashboard-connect.js',
  '/static/dashboard-import.js',
  '/static/dashboard-pwa.js',
  '/static/dashboard.js',
  '/static/dashboard-theme.js',
  '/static/dashboard-theme-cosmic.css',
  '/static/dashboard-graph-layout.js',
  '/static/dashboard-graph.js',
  '/static/dashboard-graph-atlas.js',
  '/static/dashboard-graph.css',
  '/static/manifest.json',
  '/static/elesync-main-logo.webp',
  '/static/elesync-favicon.png',
  '/static/elesync-main-logo-cosmic.webp',
  '/static/elesync-favicon-cosmic.png',
  '/static/elesync-ui-elephant-v2.webp',
  '/static/login.css',
  '/static/login.js',
  '/static/Geist-Variable.woff2',
  '/static/GeistMono-Variable.woff2',
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(key => key !== CACHE).map(key => caches.delete(key))
    ))
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET' || new URL(request.url).pathname.startsWith('/api/')) return;

  event.respondWith(
    fetch(request).then(response => {
      // Only cache successful, non-opaque responses. Caching a 4xx/5xx or a
      // redirect to /login turns transient auth failures into permanently
      // stale offline pages, and opaque cross-origin responses are useless
      // to serve from cache anyway.
      if (response && response.ok && response.type !== 'opaque') {
        const copy = response.clone();
        caches.open(CACHE).then(cache => cache.put(request, copy));
      }
      return response;
    }).catch(() => caches.match(request).then(cached => cached || caches.match('/')))
  );
});
