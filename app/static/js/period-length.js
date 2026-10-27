// SPDX-License-Identifier: AGPL-3.0-or-later
// The new-period form (D120): show only the fields the chosen length uses. Without JavaScript
// every field shows, and the server ignores the ones the length doesn't use.
(function () {
  "use strict";
  const form = document.querySelector("form[data-period-form]");
  if (!form) return;
  const uses = { weeks: ["start_date", "weeks"], months: ["start_date", "months"], term: ["term"] };
  const wrappers = {};
  ["start_date", "weeks", "months", "term"].forEach(function (name) {
    const field = form.elements.namedItem(name);
    if (field) wrappers[name] = field.closest("form > div");
  });
  function update() {
    const chosen = form.querySelector('input[name="length"]:checked');
    const shown = uses[chosen ? chosen.value : "weeks"] || [];
    Object.keys(wrappers).forEach(function (name) {
      if (wrappers[name]) wrappers[name].hidden = shown.indexOf(name) === -1;
    });
  }
  form.querySelectorAll('input[name="length"]').forEach(function (radio) {
    radio.addEventListener("change", update);
  });
  update();
})();
