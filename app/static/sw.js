// Keeps the app shell (page, styles, scripts, icons) available so the app opens instantly and
// shows a clear message when your computer is unreachable. Data (/api) is never cached.
const CACHE = "shell-v2";
const SHELL = ["/", "/static/styles.css", "/static/app.js", "/static/vendor/chart-4.4.1.umd.js",
               "/static/icons/icon-192.png", "/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/auth/") ||
      url.pathname === "/login" || url.pathname === "/logout") return;
  // Network first, so updates show up straight away; fall back to the cached shell when offline.
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok && !res.redirected) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
        return res;
      })
      .catch(() => caches.match(e.request).then((r) => r || caches.match("/")))
  );
});
