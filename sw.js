// Forge Mobile — service worker (appli installable + notifications locales)
const CACHE = "forge-mobile-v6";
const SHELL = ["/mobile/", "/mobile/manifest.webmanifest", "/mobile/icon-192.png", "/mobile/icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

// Page : réseau d'abord (toujours la dernière version), cache seulement si le PC est injoignable.
// L'API et les images ne passent jamais par le cache.
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (e.request.mode === "navigate" && url.pathname.startsWith("/mobile")) {
    e.respondWith(
      fetch(e.request).then(r => {
        const copy = r.clone();
        caches.open(CACHE).then(c => c.put("/mobile/", copy)).catch(() => {});
        return r;
      }).catch(() => caches.match("/mobile/"))
    );
  }
});

self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
    for (const c of list) if (c.url.includes("/mobile")) return c.focus();
    return self.clients.openWindow("/mobile/");
  }));
});
