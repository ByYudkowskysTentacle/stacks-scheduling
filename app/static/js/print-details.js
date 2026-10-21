// SPDX-License-Identifier: AGPL-3.0-or-later
// Printouts show every validation group in full (audit 2.12): open the closed ones while
// printing and close them again afterwards.
(function () {
  "use strict";
  let opened = [];
  window.addEventListener("beforeprint", function () {
    opened = Array.from(document.querySelectorAll(".validation details:not([open])"));
    opened.forEach(function (d) { d.open = true; });
  });
  window.addEventListener("afterprint", function () {
    opened.forEach(function (d) { d.open = false; });
    opened = [];
  });
})();
