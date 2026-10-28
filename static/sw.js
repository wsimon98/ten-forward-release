// Ten Forward service worker: cache the app shell, never cache API or media.
const SHELL = "tenforward-shell-v3";
const SHELL_FILES = ["/", "/static/app.css", "/static/app.js", "/manifest.webmanifest", "/static/icon-192.png", "/static/icon-512.png", "/static/icon-maskable-512.png"];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).catch(() => null));
  self.skipWaiting();
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/") || url.pathname.startsWith("/media/")) return;
  e.respondWith(
    fetch(e.request).then((res) => {
      if (res.ok && SHELL_FILES.includes(url.pathname)) {
        const copy = res.clone();
        caches.open(SHELL).then((c) => c.put(e.request, copy));
      }
      return res;
    }).catch(() => caches.match(e.request))
  );
});
