// SPDX-License-Identifier: AGPL-3.0-or-later
// Stacks service worker (D124). It keeps the style sheet, fonts and an offline page, and saves
// the person's home page, their shifts and the published schedule as they view them, so those
// can be read without signal. Pages always come from the network first: a saved copy is shown
// only when the network fails, with a note saying when it was saved. Signing in or out drops
// the saved pages (D122). It also shows phone notifications (D125), and signing out turns them
// off for this device. Configuration comes from app/core/pwa.py.
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
    if (url.pathname === CONFIG.logoutPath) event.waitUntil(dropPush());
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

// --- Phone notifications (D125) ---

// Every push must show a notification: Safari stops sending to a site whose pushes show none.
self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (error) {
    data = {};
  }
  const text = (value) => (typeof value === "string" ? value : "");
  const options = {
    body: text(data.body),
    icon: CONFIG.icon,
    badge: CONFIG.badge,
    data: { url: text(data.url) || "/" },
  };
  if (text(data.tag)) options.tag = data.tag;
  event.waitUntil(self.registration.showNotification(text(data.title) || CONFIG.siteName, options));
});

// A tap opens the page the notification is about, in this site only.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  let target = new URL("/", self.location.origin);
  try {
    const url = new URL((event.notification.data || {}).url || "/", self.location.origin);
    if (url.origin === self.location.origin) target = url;
  } catch (error) {
    // keep the home page
  }
  event.waitUntil(
    // Only windows this worker controls: navigate() refuses the others.
    self.clients.matchAll({ type: "window" }).then((windows) => {
      const open = windows.find((client) => client.url === target.href && "navigate" in client);
      if (!open) return self.clients.openWindow(target.href);
      // Focus while the tap still allows it, then load the page again so it shows what the
      // notification is about, not what it showed before.
      return open.focus()
        .then((focused) => (focused || open).navigate(target.href))
        .catch(() => self.clients.openWindow(target.href));
    }),
  );
});

// The browser replaced the subscription: tell the server, proving it with the old one's secret.
self.addEventListener("pushsubscriptionchange", (event) => {
  const old = event.oldSubscription;
  if (!CONFIG.push || !old) return;
  const fresh = event.newSubscription
    ? Promise.resolve(event.newSubscription)
    : self.registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: keyBytes(CONFIG.push.publicKey),
    });
  event.waitUntil(
    fresh
      .then((subscription) => postJson(CONFIG.push.renewUrl, {
        old: { endpoint: old.endpoint, auth: authOf(old) },
        subscription: subscription.toJSON(),
      }))
      .catch(() => {}),
  );
});

// Signing out turns notifications off for this device, here and on the server.
function dropPush() {
  if (!CONFIG.push) return Promise.resolve();
  return self.registration.pushManager.getSubscription()
    .then((subscription) => {
      if (!subscription) return undefined;
      return postJson(CONFIG.push.dropUrl, { endpoint: subscription.endpoint, auth: authOf(subscription) })
        .catch(() => {})
        .then(() => subscription.unsubscribe());
    })
    .catch(() => {});
}

function postJson(url, data) {
  return fetch(url, {
    method: "POST",
    credentials: "omit",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

function authOf(subscription) {
  return ((subscription.toJSON() || {}).keys || {}).auth || "";
}

function keyBytes(text) {
  const base64 = (text + "=".repeat((4 - (text.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
}
