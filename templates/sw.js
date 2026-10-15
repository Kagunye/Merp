/* mofisi service worker — offline shell + outbox for POST actions.
   Strategy:
     - precache the app shell (base CSS, logo, manifest, offline page)
     - network-first for navigations, falling back to offline.html
     - stale-while-revalidate for static /static/* assets
     - POST requests get queued in IndexedDB when offline and replayed
       on the next sync event via the mofisi-outbox tag
*/
const CACHE = 'mofisi-v1';
const SHELL = [
  '/offline/',
  '/static/css/tailwind.css',
  '/static/img/mofisi-mark.svg',
  '/static/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

/* ---------- IndexedDB outbox for POSTs taken offline ---------- */
function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open('mofisi-outbox', 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore('queue', { keyPath: 'id', autoIncrement: true });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function enqueue(req) {
  const body = await req.clone().text();
  const headers = {};
  req.headers.forEach((v, k) => { headers[k] = v; });
  const db = await openDB();
  const tx = db.transaction('queue', 'readwrite');
  tx.objectStore('queue').add({
    url: req.url, method: req.method, headers, body, ts: Date.now()
  });
  return new Promise((r, j) => { tx.oncomplete = r; tx.onerror = () => j(tx.error); });
}

async function flush() {
  const db = await openDB();
  const tx = db.transaction('queue', 'readwrite');
  const store = tx.objectStore('queue');
  const all = await new Promise((r) => { const req = store.getAll(); req.onsuccess = () => r(req.result); });
  for (const item of all) {
    try {
      const res = await fetch(item.url, { method: item.method, headers: item.headers, body: item.body });
      if (res.ok || res.status < 500) {
        store.delete(item.id);
      }
    } catch (e) { /* still offline, keep queued */ }
  }
}

self.addEventListener('sync', (event) => {
  if (event.tag === 'mofisi-outbox') event.waitUntil(flush());
});

/* ---------- Fetch router ---------- */
self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Static assets: stale-while-revalidate
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.open(CACHE).then(async (c) => {
        const cached = await c.match(req);
        const fetchP = fetch(req).then((res) => { if (res && res.status === 200) c.put(req, res.clone()); return res; }).catch(() => cached);
        return cached || fetchP;
      })
    );
    return;
  }

  // GET navigations: network-first, fall back to cache or offline page
  if (req.method === 'GET' && req.headers.get('accept') && req.headers.get('accept').includes('text/html')) {
    event.respondWith(
      fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      }).catch(async () => (await caches.match(req)) || caches.match('/offline/'))
    );
    return;
  }

  // POST: queue when offline, then self-redirect so the browser doesn't error
  if (req.method === 'POST') {
    event.respondWith(
      fetch(req.clone()).catch(async () => {
        await enqueue(req);
        if ('sync' in self.registration) {
          try { await self.registration.sync.register('mofisi-outbox'); } catch (e) {}
        }
        return new Response(
          '<!doctype html><meta charset=utf-8><title>Queued offline</title>' +
          '<div style="font-family:system-ui;max-width:480px;margin:40px auto;padding:24px;' +
          'border:1px solid #E5E7EB;border-radius:12px;background:#FFFBEB;color:#78350F;">' +
          '<h2 style="margin:0 0 10px;color:#92400E;">Saved offline</h2>' +
          '<p>You are offline. mofisi has queued this action and will send it when the connection returns.</p>' +
          '<p><a href="/" style="color:#DC2538;font-weight:600;">Back to dashboard</a></p></div>',
          { status: 202, headers: { 'Content-Type': 'text/html' } }
        );
      })
    );
  }
});
