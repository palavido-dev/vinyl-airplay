const CACHE_NAME = 'vinyl-streamer-v3';
const STATIC_ASSETS = [
  '/manifest.json',
  '/static/css/app.css',
];

self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(STATIC_ASSETS).catch(err => {
        console.log('Cache addAll error:', err);
      });
    })
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.map(cacheName => {
          if (cacheName !== CACHE_NAME) {
            return caches.delete(cacheName);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // Never cache API / auth / websocket upgrades — sessions and CSRF must stay live.
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/ws') ||
    url.pathname.startsWith('/live.mp3')
  ) {
    event.respondWith(fetch(event.request));
    return;
  }

  // Network-first for the shell
  if (url.pathname === '/') {
    event.respondWith(
      fetch(event.request).catch(() =>
        caches.match(event.request).then(cached =>
          cached || new Response('Offline', { status: 503 })
        )
      )
    );
    return;
  }

  // Static assets: cache-first with network fallback
  event.respondWith(
    caches.match(event.request).then(response => {
      return response || fetch(event.request);
    })
  );
});
