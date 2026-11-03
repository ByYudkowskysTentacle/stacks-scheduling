// SPDX-License-Identifier: AGPL-3.0-or-later
// Installable on phones (D124). Registers the service worker (/sw.js), and drives the
// "Put Stacks on your phone" card on the home page: on Android it offers the browser's own
// Install button; on iPhone and iPad, which never prompt, it links to the step-by-step help.
// The card stays hidden once the app is installed, or after "Not now" on this device.
(function () {
  "use strict";

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(function () { /* offline copies are a bonus */ });
  }

  var DISMISSED = "stacks-install-card-dismissed";
  var card = document.querySelector("[data-install-card]");
  if (!card) return;

  var standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  var dismissed = false;
  try { dismissed = localStorage.getItem(DISMISSED) === "1"; } catch (e) { /* storage blocked */ }
  if (standalone || dismissed) return;

  var apple = /iPhone|iPad|iPod/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1); // iPadOS reports a Mac

  function show(kind) {
    card.querySelectorAll("[data-install-for]").forEach(function (el) {
      el.hidden = el.getAttribute("data-install-for") !== kind;
    });
    card.hidden = false;
  }

  if (apple) show("apple");

  var prompt = null;
  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault(); // show our card instead of the browser's mini bar
    prompt = event;
    show("prompt");
  });
  var install = card.querySelector("[data-install-button]");
  if (install) {
    install.addEventListener("click", function () {
      if (!prompt) return;
      prompt.prompt();
      prompt.userChoice.finally(function () { prompt = null; card.hidden = true; });
    });
  }
  window.addEventListener("appinstalled", function () { card.hidden = true; });

  var notNow = card.querySelector("[data-install-dismiss]");
  if (notNow) {
    notNow.addEventListener("click", function () {
      try { localStorage.setItem(DISMISSED, "1"); } catch (e) { /* storage blocked */ }
      card.hidden = true;
    });
  }
})();
