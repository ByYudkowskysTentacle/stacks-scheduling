// SPDX-License-Identifier: AGPL-3.0-or-later
// Phone notifications (D125). Loaded on signed-in pages when the site has push set up.
// - On every page: once a day, and as soon as someone else signs in on this device, ask the
//   server whether this device's notifications belong to the person signed in. If not, the
//   server has dropped them and this turns them off here too, so the next person on a shared
//   phone never gets the last person's news.
// - On Notification settings: "Turn on notifications on this device" and its states. iPhone and
//   iPad can only get them in the Home Screen app (iOS 16.4 or later), so Safari gets a pointer
//   to the install steps instead.
(function () {
  "use strict";

  var CHECKED = "stacks-push-checked";
  var body = document.body;
  var csrf = body.getAttribute("data-csrf") || "";
  var supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

  var CONFIRMED = "stacks-push-confirmed"; // the endpoint the server last said was this person's

  function stored(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      localStorage.setItem(key, value);
    } catch (e) { /* storage blocked: check again next time */ }
    return null;
  }

  function remember(value) {
    return stored(CHECKED, value);
  }

  function post(url, data) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify(data),
    });
  }

  function authOf(subscription) {
    return ((subscription.toJSON() || {}).keys || {}).auth || "";
  }

  function keyBytes(text) {
    var base64 = (text + "=".repeat((4 - (text.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
    return Uint8Array.from(atob(base64), function (c) { return c.charCodeAt(0); });
  }

  function sameKey(subscription, key) {
    var used = subscription.options && subscription.options.applicationServerKey;
    if (!used) return true;
    var bytes = new Uint8Array(used);
    if (bytes.length !== key.length) return false;
    for (var i = 0; i < key.length; i += 1) if (bytes[i] !== key[i]) return false;
    return true;
  }

  // --- The device check, on every signed-in page ---

  var checked = Promise.resolve();
  var checkUrl = body.getAttribute("data-push-check");
  var mark = (body.getAttribute("data-push-user") || "") + ":" + new Date().toISOString().slice(0, 10);
  var box = document.querySelector("[data-push]");
  var pending = body.hasAttribute("data-push-pending"); // one of the person's devices awaits them
  if (supported && checkUrl) {
    checked = navigator.serviceWorker.ready
      .then(function (registration) { return registration.pushManager.getSubscription(); })
      .then(function (subscription) {
        // Notification settings always asks, so it never says "on" for a device the server
        // dropped. Other pages ask once a day, or at once if this is the device awaiting
        // its owner: a renewal always brings a new endpoint, not the one confirmed here.
        var waiting = pending && subscription && stored(CONFIRMED) !== subscription.endpoint;
        if (!box && !waiting && remember() === mark) return false;
        if (!subscription) return true;
        return post(checkUrl, { endpoint: subscription.endpoint, auth: authOf(subscription) })
          .then(function (response) { return response.ok ? response.json() : null; })
          .then(function (answer) {
            if (!answer) return false;
            if (answer.state === "off") return subscription.unsubscribe().then(function () { return true; });
            stored(CONFIRMED, subscription.endpoint);
            return true;
          });
      })
      .then(function (done) { if (done) remember(mark); })
      .catch(function () { /* try again on the next page */ });
  }

  // --- Notification settings ---

  if (!box) return;
  var message = box.querySelector("[data-push-message]");

  function show(state) {
    box.querySelectorAll("[data-push-state]").forEach(function (el) {
      el.hidden = el.getAttribute("data-push-state") !== state;
    });
  }

  function say(text) {
    if (message) message.textContent = text;
  }

  var apple = /iPhone|iPad|iPod/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1); // iPadOS reports a Mac
  var standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  if (!supported) {
    show(apple && !standalone ? "install" : "unsupported");
    return;
  }
  if (Notification.permission === "denied") {
    show("blocked");
    return;
  }

  var key = keyBytes(box.getAttribute("data-public-key") || "");
  var dropUrl = box.getAttribute("data-drop-url");
  var registration = null;
  // `ready` never fails: if the service worker can't start, say so after a while (and still
  // let a late start replace the message).
  var settled = false;
  var stuck = window.setTimeout(function () { if (!settled) show("stuck"); }, 15000);

  function settle(state) {
    settled = true;
    window.clearTimeout(stuck);
    show(state);
  }

  function focusOn(state) {
    var el = box.querySelector('[data-push-state="' + state + '"]');
    if (el) el.focus();
  }

  function drop(subscription) {
    return post(dropUrl, { endpoint: subscription.endpoint, auth: authOf(subscription) })
      .catch(function () { /* the server drops it on the next refusal anyway */ })
      .then(function () { return subscription.unsubscribe(); });
  }

  checked
    .then(function () { return navigator.serviceWorker.ready; })
    .then(function (ready) {
      registration = ready;
      return ready.pushManager.getSubscription();
    })
    .then(function (subscription) {
      if (subscription && !sameKey(subscription, key)) {
        // The site's key has changed since this device subscribed: start again.
        return drop(subscription).then(function () { settle("off"); });
      }
      settle(subscription ? "on" : "off");
      return undefined;
    })
    .catch(function () { settle("unsupported"); });

  var on = box.querySelector("[data-push-on]");
  if (on) {
    on.addEventListener("click", function () {
      if (!registration) return;
      say("");
      on.disabled = true;
      // Called straight from the tap: Safari asks permission only in answer to one.
      registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key })
        .then(function (subscription) {
          return post(box.getAttribute("data-subscribe-url"), subscription.toJSON()).then(function (response) {
            if (!response.ok) {
              return subscription.unsubscribe().then(function () { throw new Error("refused"); });
            }
            remember(mark);
            window.location.reload(); // the device list now includes this one
            return undefined;
          });
        })
        .catch(function () {
          on.disabled = false;
          if (Notification.permission === "denied") {
            show("blocked");
            focusOn("blocked"); // the button went with its panel: move focus to what replaced it
          } else {
            say("Notifications couldn't be turned on. Try again, or check this browser's notification settings.");
          }
        });
    });
  }

  var off = box.querySelector("[data-push-off]");
  if (off) {
    off.addEventListener("click", function () {
      if (!registration) return;
      say("");
      off.disabled = true;
      registration.pushManager.getSubscription()
        .then(function (subscription) { return subscription ? drop(subscription) : undefined; })
        .then(function () { window.location.reload(); })
        .catch(function () {
          off.disabled = false;
          say("Notifications couldn't be turned off. Try again.");
        });
    });
  }
})();
