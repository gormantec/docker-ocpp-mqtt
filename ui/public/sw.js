const CACHE_VERSION = "ocpp-pwa-v1";
const APP_SCOPE = "/webapp/ocpp-mqtt/";
const APP_SHELL = [
  APP_SCOPE,
  `${APP_SCOPE}index.html`,
  `${APP_SCOPE}manifest.webmanifest`,
  `${APP_SCOPE}aws-console.css`,
  `${APP_SCOPE}icons/icon-192.svg`,
  `${APP_SCOPE}icons/icon-512.svg`,
  `${APP_SCOPE}icons/maskable-512.svg`,
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(APP_SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key !== CACHE_VERSION).map((key) => caches.delete(key))
    )).then(() => self.clients.claim())
  );
});

const isApiRequest = (url) => {
  const path = url.pathname;
  return path.endsWith("/debug") || path.endsWith("/schedule") || path.endsWith("/health") || path.endsWith("/settings") || path.endsWith("/override");
};

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);

  if (request.method !== "GET" || url.origin !== self.location.origin || isApiRequest(url)) {
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const clone = response.clone();
          caches.open(CACHE_VERSION).then((cache) => cache.put(request, clone));
          return response;
        })
        .catch(async () => {
          const cached = await caches.match(request);
          if (cached) return cached;
          return caches.match(`${APP_SCOPE}index.html`);
        })
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request).then((response) => {
        const clone = response.clone();
        caches.open(CACHE_VERSION).then((cache) => cache.put(request, clone));
        return response;
      });
    })
  );
});
