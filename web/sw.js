// Free Mail service worker.
// - app shell: network first, cached copy when the local server is stopped
// - offline mode: the last folder lists, messages and conversations read are kept, and served
//   (flagged with X-FM-Cache: 1) when the server cannot reach the mailbox or is unreachable
const SHELL = 'fm-shell-v2';
const DATA = 'fm-data-v1';
const ASSETS = ['/', '/offline.html', '/manifest.webmanifest', '/icons/icon-192.png', '/icons/badge-96.png'];
const CACHEABLE = /^\/api\/mail\/(list|msg|folders|thread)$/;
const MAX_ENTRIES = 400;
let puts = 0;

self.addEventListener('install', e => { e.waitUntil(caches.open(SHELL).then(c => c.addAll(ASSETS)).catch(() => {})); self.skipWaiting(); });
self.addEventListener('activate', e => e.waitUntil((async () => {
  for (const k of await caches.keys()) if (k !== SHELL && k !== DATA) await caches.delete(k);
  await self.clients.claim();
})()));
self.addEventListener('message', e => { if (e.data && e.data.type === 'clear') e.waitUntil(caches.delete(DATA)); });

function cacheKey(req) {
  const u = new URL(req.url);
  ['vis', 'seen', '_'].forEach(p => u.searchParams.delete(p));
  return u.toString();
}
async function fromCache(key) {
  const r = await (await caches.open(DATA)).match(key);
  if (!r) return null;
  const h = new Headers(r.headers); h.set('X-FM-Cache', '1');
  return new Response(await r.blob(), { status: 200, headers: h });
}
async function trim(c) {
  const keys = await c.keys();
  for (let i = 0; i < keys.length - MAX_ENTRIES; i++) await c.delete(keys[i]);
}
async function networkFirst(req) {
  const key = cacheKey(req);
  try {
    const r = await fetch(req);
    if (r.ok) {
      const c = await caches.open(DATA);
      await c.delete(key); await c.put(key, r.clone());   // delete first: keys() stays in recency order
      if (++puts % 25 === 0) trim(c);
      return r;
    }
    if (r.status >= 500) return (await fromCache(key)) || r;   // mailbox unreachable (no network)
    return r;                                                   // 401/403/404: never hide auth errors
  } catch (err) {
    const hit = await fromCache(key);
    if (hit) return hit;
    throw err;
  }
}

self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (u.origin !== location.origin) return;
  if (u.pathname.startsWith('/api/')) {
    if (e.request.method === 'GET' && CACHEABLE.test(u.pathname)) e.respondWith(networkFirst(e.request));
    return;
  }
  if (e.request.mode === 'navigate') {
    // network first: the server is the source of truth; fall back to a "server stopped" page
    e.respondWith(fetch(e.request).then(r => { caches.open(SHELL).then(c => c.put('/', r.clone())); return r; })
      .catch(() => caches.match('/offline.html')));
    return;
  }
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});
// notifications raised by the page itself (Android has no `new Notification()`)
self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = e.notification.data && e.notification.data.url || '/';
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of all) { if ('focus' in c) { await c.focus(); c.postMessage({ type: 'open', url }); return; } }
    await self.clients.openWindow(url);
  })());
});
