/* 모아타 서비스 워커: 앱 화면(HTML·CSS·JS·아이콘)만 저장해 두고, API는 항상 네트워크로 보낸다. */
const CACHE = 'moata-v2';
const SHELL = [
  '/passenger', '/driver', '/static/moata.css', '/static/common.js', '/static/passenger.js', '/static/driver.js',
  '/static/vendor/leaflet/leaflet.js', '/static/vendor/leaflet/leaflet.css', '/static/icons/icon.svg',
  '/static/icons/icon-192.png', '/static/icons/icon-512.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  // 화면은 네트워크 먼저(최신 코드), 안 되면 저장본
  e.respondWith(
    fetch(e.request, { cache: 'no-cache' })
      .then((res) => {
        if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
        return res;
      })
      .catch(() => caches.match(e.request).then((hit) => hit || caches.match('/passenger'))),
  );
});
