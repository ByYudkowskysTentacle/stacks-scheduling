// SPDX-License-Identifier: AGPL-3.0-or-later
// Paintable weekly availability grid. Progressive enhancement only: the text boxes in the
// form are the source of truth (and the accessible way to enter times); this grid reads
// them, and writes back only the day being painted. Slots are 15 minutes (SPEC §3.3).

const SLOT = 15;
const DAY = 1440;

function parseClock(text) {
  const t = text.trim().toLowerCase();
  if (t === "noon") return 720;
  if (t === "midnight") return DAY;
  const m = t.match(/^(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m?\.?$/) || t.match(/^(\d{1,2})(?::(\d{2}))?$/);
  if (!m) return null;
  let h = Number(m[1]);
  const min = Number(m[2] || 0);
  if (min >= 60) return null;
  if (m[3]) {
    if (h < 1 || h > 12) return null;
    h = (h % 12) + (m[3] === "p" ? 12 : 0);
  }
  const v = h * 60 + min;
  return v <= DAY ? v : null;
}

function parseRanges(text) {
  const t = text.trim().toLowerCase();
  if (["all day", "all-day", "allday", "all"].includes(t)) return [[0, DAY]];
  const out = [];
  for (const part of t.split(/[,;]/)) {
    if (!part.trim()) continue;
    const bits = part.split(/\s*(?:-|–|\bto\b)\s*/);
    if (bits.length !== 2) return null;
    const a = parseClock(bits[0]);
    const b = parseClock(bits[1]);
    if (a === null || b === null || a >= b) return null;
    out.push([a, b]);
  }
  return merge(out);
}

function merge(ranges) {
  const sorted = ranges.slice().sort((x, y) => x[0] - y[0]);
  const out = [];
  for (const [a, b] of sorted) {
    if (out.length && a <= out[out.length - 1][1]) out[out.length - 1][1] = Math.max(out[out.length - 1][1], b);
    else out.push([a, b]);
  }
  return out;
}

function fmt(m) {
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
}

function formatRanges(ranges) {
  const merged = merge(ranges);
  if (merged.length === 1 && merged[0][0] === 0 && merged[0][1] === DAY) return "all day";
  return merged.map(([a, b]) => `${fmt(a)}-${fmt(b)}`).join(", ");
}

function clipOutside(ranges, lo, hi) {
  const out = [];
  for (const [a, b] of ranges) {
    if (a < lo) out.push([a, Math.min(b, lo)]);
    if (b > hi) out.push([Math.max(a, hi), b]);
  }
  return out;
}

function init() {
  const root = document.getElementById("availability-painter");
  const configEl = document.getElementById("painter-config");
  if (!root || !configEl) return;
  const cfg = JSON.parse(configEl.textContent);
  const nSlots = (cfg.end - cfg.start) / SLOT;
  const inputs = {};
  document.querySelectorAll("input[data-weekday][data-kind]").forEach((el) => {
    inputs[`${el.dataset.kind}_${el.dataset.weekday}`] = el;
  });
  const state = {};
  let brush = "unavailable";
  let anchor = null;
  let painting = false;
  let dirtyDay = null;
  let lastSlot = null;

  function isOpen(wd, slot) {
    const m = cfg.start + slot * SLOT;
    return (cfg.open[wd] || []).some(([a, b]) => a <= m && m < b);
  }

  function readDay(wd) {
    const un = parseRanges(inputs[`unavailable_${wd}`].value) || [];
    const pref = parseRanges(inputs[`preferred_${wd}`].value) || [];
    const row = new Array(nSlots).fill("");
    for (let s = 0; s < nSlots; s++) {
      const a = cfg.start + s * SLOT;
      const b = a + SLOT;
      if (un.some(([x, y]) => x < b && a < y)) row[s] = "u";
      else if (pref.some(([x, y]) => x <= a && b <= y)) row[s] = "p";
    }
    state[wd] = row;
  }

  function writeDay(wd) {
    for (const [kind, code] of [["unavailable", "u"], ["preferred", "p"]]) {
      const input = inputs[`${kind}_${wd}`];
      const kept = clipOutside(parseRanges(input.value) || [], cfg.start, cfg.end);
      const painted = [];
      state[wd].forEach((v, s) => {
        if (v === code) painted.push([cfg.start + s * SLOT, cfg.start + (s + 1) * SLOT]);
      });
      input.value = formatRanges([...kept, ...painted]);
    }
  }

  const grid = root.querySelector(".painter__grid");
  const table = document.createElement("table");
  table.className = "painter__table";
  const head = table.createTHead().insertRow();
  head.insertCell().textContent = "";
  for (const wd of cfg.weekdays) head.insertCell().textContent = cfg.names[wd];
  const body = table.createTBody();
  for (let s = 0; s < nSlots; s++) {
    const tr = body.insertRow();
    const m = cfg.start + s * SLOT;
    const label = tr.insertCell();
    label.className = "painter__time";
    if (m % 60 === 0) {
      const h = Math.floor(m / 60);
      label.textContent = `${h % 12 || 12}${h < 12 || h === 24 ? "am" : "pm"}`;
    }
    for (const wd of cfg.weekdays) {
      const td = tr.insertCell();
      td.dataset.weekday = wd;
      td.dataset.slot = s;
      if (!isOpen(wd, s)) td.classList.add("cell--closed");
      if (m % 60 === 0) td.classList.add("cell--hour");
    }
  }
  grid.appendChild(table);

  function render(wd) {
    table.querySelectorAll(`td[data-weekday="${wd}"]`).forEach((td) => {
      const v = state[wd][Number(td.dataset.slot)];
      td.classList.toggle("cell--unavailable", v === "u");
      td.classList.toggle("cell--preferred", v === "p");
      td.classList.toggle("cell--anchor", anchor !== null && anchor.wd === wd && anchor.slot === Number(td.dataset.slot));
    });
  }

  function paint(wd, from, to) {
    const code = { unavailable: "u", preferred: "p", clear: "" }[brush];
    for (let s = Math.min(from, to); s <= Math.max(from, to); s++) state[wd][s] = code;
    render(wd);
  }

  for (const wd of cfg.weekdays) {
    readDay(wd);
    render(wd);
    for (const kind of ["unavailable", "preferred"]) {
      inputs[`${kind}_${wd}`].addEventListener("change", () => {
        readDay(wd);
        render(wd);
      });
    }
  }

  table.addEventListener("pointerdown", (e) => {
    const td = e.target.closest("td[data-weekday]");
    if (!td) return;
    const wd = Number(td.dataset.weekday);
    const slot = Number(td.dataset.slot);
    if (e.pointerType === "mouse") {
      e.preventDefault();
      painting = true;
      dirtyDay = wd;
      lastSlot = slot;
      paint(wd, slot, slot);
      return;
    }
    // Touch and pen: tap the start, then the end, so the page can still scroll.
    if (anchor && anchor.wd === wd) {
      const from = anchor.slot;
      anchor = null;
      paint(wd, from, slot);
      writeDay(wd);
    } else {
      const previous = anchor;
      anchor = { wd, slot };
      if (previous) render(previous.wd);
      render(wd);
    }
  });
  table.addEventListener("pointerover", (e) => {
    if (!painting) return;
    const td = e.target.closest("td[data-weekday]");
    if (!td || Number(td.dataset.weekday) !== dirtyDay) return;
    // Fill from the last painted cell: a quick drag skips cells between pointer events.
    const slot = Number(td.dataset.slot);
    paint(dirtyDay, lastSlot, slot);
    lastSlot = slot;
  });
  window.addEventListener("pointerup", () => {
    if (painting && dirtyDay !== null) writeDay(dirtyDay);
    painting = false;
    dirtyDay = null;
  });

  root.querySelectorAll("[data-brush]").forEach((btn) => {
    btn.addEventListener("click", () => {
      brush = btn.dataset.brush;
      root.querySelectorAll("[data-brush]").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
    });
  });
  root.hidden = false;
}

init();
