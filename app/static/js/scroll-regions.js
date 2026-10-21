// SPDX-License-Identifier: AGPL-3.0-or-later
// Tables that scroll sideways on small screens must be reachable by keyboard (WCAG 2.1.1).
// Give a scrolling table or grid a tab stop only while it actually scrolls.
(function () {
  "use strict";
  function update() {
    document.querySelectorAll(".data-table, .grid-scroll, pre.sample").forEach(function (el) {
      if (el.scrollWidth > el.clientWidth + 1) {
        el.setAttribute("tabindex", "0");
      } else if (el.getAttribute("tabindex") === "0") {
        el.removeAttribute("tabindex");
      }
    });
  }
  document.addEventListener("DOMContentLoaded", update);
  window.addEventListener("resize", update);
})();
