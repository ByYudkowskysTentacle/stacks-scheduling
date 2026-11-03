// SPDX-License-Identifier: AGPL-3.0-or-later
// Stacks service worker (D124). It keeps the style sheet, fonts and an offline page, and saves
// the person's home page, their shifts and the published schedule as they view them, so those
// can be read without signal. Pages always come from the network first: a saved copy is shown
// only when the network fails, with a note saying when it was saved. Signing in or out drops
// the saved pages (D122). Configuration comes from app/core/pwa.py.
"use strict";

const CONFIG = {{ config|safe }};
const STATIC_CACHE = "stacks-static-" + CONFIG.version;
const PAGES_CACHE = "stacks-pages";
const OFFLINE_NOTE = "data-offline-note hidden"; // in base.html; the hidden note is revealed

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then((cache) => cache.addAll(CONFIG.precache))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names
          .filter((name) => name.startsWith("stacks-static-") && name !== STATIC_CACHE)
          .map((name) => caches.delete(name)),
      ))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.method === "POST" && (url.pathname === CONFIG.loginPath || url.pathname === CONFIG.logoutPath)) {
    event.waitUntil(caches.delete(PAGES_CACHE)); // someone else may use the phone next
    return;
  }
  if (request.method !== "GET") return;
  if (request.mode === "navigate") {
    event.respondWith(page(request, url));
  } else if (url.pathname.startsWith(CONFIG.staticPrefix)) {
    event.respondWith(caches.match(request).then((hit) => hit || fetch(request)));
  }
});

async function page(request, url) {
  const saved = CONFIG.savedPages.includes(url.pathname);
  try {
    const response = await fetch(request);
    if (saved && response.type === "opaqueredirect") {
      await caches.delete(PAGES_CACHE); // sent to sign in: the session has ended
    } else if (saved && response.ok) {
      const cache = await caches.open(PAGES_CACHE);
      await cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    const copy = saved && (await caches.match(request, { cacheName: PAGES_CACHE, ignoreVary: true }));
    if (copy) return markOffline(copy);
    const offline = await caches.match(CONFIG.offlineUrl, { ignoreVary: true });
    if (offline) return offline;
    throw error;
  }
}

async function markOffline(response) {
  const html = (await response.text()).replace(OFFLINE_NOTE, "data-offline-note");
  return new Response(html, {
    status: 200,
    headers: { "Content-Type": response.headers.get("Content-Type") || "text/html; charset=utf-8" },
  });
}
