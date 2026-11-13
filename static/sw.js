/* Mofisi offline service worker (served at /sw.js, see apps/core/offline.py).
 *
 * - The app's own files: fresh from the server when online, the kept copy offline;
 *   outside libraries and fonts: from the cache.
 * - Pages: from the network, kept as they were last seen; offline, the last
 *   copy is shown, or the offline page for a page never opened.
 * - Nothing that changes data (POST) is ever cached; sign-in, sign-out and
 *   the admin are always live. Signing out empties the page cache.
 */
const VERSION = 'merp-offline-v2';
const FILES = 'files-' + VERSION;
const PAGES = 'pages-' + VERSION;
const OFFLINE_URL = '/offline/';
const LIVE_ONLY = [/^\/auth\//, /^\/admin\//, /^\/kra\/cron\//, /^\/ping\//, /^\/sw\.js$/];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(PAGES).then((c) => c.add(new Request(OFFLINE_URL, { cache: 'reload' }))).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key !== FILES && key !== PAGES) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

self.addEventListener('message', (event) => {
  if (event.data === 'clear-pages') {
    event.waitUntil(caches.delete(PAGES).then(() => caches.open(PAGES)).then((c) => c.add(OFFLINE_URL)).catch(() => {}));
  }
});

function isFile(url) {
  return url.pathname.startsWith('/static/') || url.hostname.endsWith('fonts.googleapis.com')
    || url.hostname.endsWith('fonts.gstatic.com') || url.hostname === 'unpkg.com'
    || url.hostname === 'cdn.jsdelivr.net';
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;                      // POSTs go straight through
  const url = new URL(req.url);
  if (url.origin === self.location.origin && LIVE_ONLY.some((re) => re.test(url.pathname))) return;

  if (url.origin === self.location.origin && url.pathname.startsWith('/static/')) {
    // Our own files: always the newest copy while online (a stale till.js
    // would be a till without its latest fixes); the kept copy when offline.
    event.respondWith((async () => {
      const cache = await caches.open(FILES);
      try {
        const res = await fetch(req, { cache: 'no-cache' });
        if (res && res.ok) cache.put(req, res.clone());
        return res;
      } catch (err) {
        return (await cache.match(req)) || Response.error();
      }
    })());
    return;
  }
  if (isFile(url)) {                                     // libraries and fonts: cache first
    event.respondWith((async () => {
      const cache = await caches.open(FILES);
      const hit = await cache.match(req);
      const fresh = fetch(req).then((res) => {
        if (res && (res.ok || res.type === 'opaque')) cache.put(req, res.clone());
        return res;
      }).catch(() => null);
      return hit || (await fresh) || Response.error();
    })());
    return;
  }
  if (url.origin !== self.location.origin) return;

  const page = req.mode === 'navigate' || (req.headers.get('accept') || '').includes('text/html');
  event.respondWith((async () => {
    const cache = await caches.open(PAGES);
    try {
      const res = await fetch(req);
      // Keep good, same-site answers; never a redirect to the sign-in page.
      if (res.ok && res.type === 'basic' && !res.redirected) cache.put(req, res.clone());
      return res;
    } catch (err) {
      const hit = await cache.match(req, { ignoreVary: true });
      if (hit) return hit;
      if (page) return (await cache.match(OFFLINE_URL)) || new Response('Offline', { status: 503 });
      return new Response(JSON.stringify({ ok: false, offline: true }), {
        status: 503, headers: { 'Content-Type': 'application/json' } });
    }
  })());
});
