// Margin service worker: the app shell opens offline. /api/* always goes to the network.
// pdf.js (vendor/pdfjs/) is not in the shell: it is cached the first time a PDF is imported.
const VERSION = 'margin-v19';
const SHELL = [
  './', 'index.html', 'styles.css', 'manifest.webmanifest',
  'app.js', 'ink.js', 'assistant.js', 'voice.js', 'store.js', 'shelf.js', 'pages.js', 'zoom.js', 'export.js', 'ui.js', 'images.js', 'handwriting.js', 'suggest.js', 'shapes.js', 'refine.js',
  'vendor/writey/glyphs.js', 'vendor/writey/glyphs_print.js', 'vendor/writey/font.js', 'vendor/writey/latex.js', 'vendor/writey/layout.js', 'vendor/writey/text.js', 'vendor/writey/hand.js',
  'vendor/perfect-freehand.js', 'vendor/jspdf.umd.min.js',
  'icons/icon-192.png', 'icons/icon-512.png', 'icons/apple-touch-icon.png', 'icons/favicon-32.png',
];
const FONTS = /^https:\/\/fonts\.(googleapis|gstatic)\.com\//;

self.addEventListener('install', e => {
  // cache: 'reload' skips the HTTP cache, so a new VERSION never stores yesterday's files.
  e.waitUntil(caches.open(VERSION)
    .then(c => c.addAll(SHELL.map(u => new Request(u, { cache: 'reload' }))))
    .then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;                       // POST /api/assist etc.: straight to the network
  const url = new URL(req.url);
  if (url.origin === location.origin && url.pathname.startsWith('/api/')) return;
  if (url.origin !== location.origin && !FONTS.test(req.url)) return;
  // Network-first for same-origin app shell (always fresh when online, instant cache fallback offline);
  // cache-first for external fonts.
  e.respondWith(caches.open(VERSION).then(async cache => {
    const key = req.mode === 'navigate' ? 'index.html' : req;
    const hit = await cache.match(key, { ignoreSearch: req.mode === 'navigate' });
    if (url.origin !== location.origin && hit) return hit;
    const refresh = url.origin === location.origin ? fetch(req.url, { cache: 'no-cache' }) : fetch(req);
    return refresh.then(res => {
      if (res && (res.ok || res.type === 'opaque')) cache.put(key, res.clone());
      return res;
    }).catch(() => hit || new Response('Offline', { status: 503 }));
  }));
});
