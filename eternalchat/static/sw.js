// EternalChat's service worker: the page opens without a network (it then waits
// to reconnect), and a reply that arrives while the page is in the background
// can notify. The chat itself is never cached: /api/ always goes to the server.
const SHELL = "eternalchat-shell-v2";
const FILES = ["/", "/manifest.webmanifest", "/icon.svg", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((cache) => cache.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((key) => key !== SHELL).map((key) => caches.delete(key))))
    .then(() => self.clients.claim()));
});

// The page: the network first, so an upgrade shows at once; the saved copy offline.
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  const path = event.request.mode === "navigate" ? "/" : url.pathname;
  if (!FILES.includes(path)) return;
  event.respondWith(fetch(event.request).then((response) => {
    if (response.ok) {
      const copy = response.clone();
      caches.open(SHELL).then((cache) => cache.put(path, copy));
    }
    return response;
  }).catch(() => caches.match(path).then((saved) => saved || Response.error())));
});

// A notification opens the chat: the window already showing it, else a new one.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
    const open = windows.find((w) => new URL(w.url).origin === location.origin);
    return open ? open.focus() : self.clients.openWindow("/");
  }));
});

// Generic, encrypted Web Push payloads contain no chat text, links or credentials.
self.addEventListener("push", event => {
  event.waitUntil((async () => {
    let id = "reply";
    try { id = String(event.data?.json()?.id || id).slice(0, 80); } catch {}
    await self.registration.showNotification("EternalChat", {
      body:"A reply is ready.", tag:"eternalchat-reply", icon:"/icon-192.png", badge:"/icon-192.png", data:{id}
    });
  })());
});
self.addEventListener("pushsubscriptionchange", event => {
  // Credentials remain in browser storage, never in the worker. Re-enroll on opening.
  event.waitUntil(self.clients.matchAll({type:"window"}).then(windows=>{
    for (const client of windows) client.postMessage({kind:"push-subscription-changed"});
  }));

});
