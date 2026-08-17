"use strict";

const state = {
  filters: {
    host: [], status: [], method: [], country: [],
    ip: "", q: "", hide_uptime: false, hide_local: false, from: "", to: "",
    simplified: false,
  },
  sort: "ts",
  order: "desc",
  page: 1,
  size: 50,
  live: true,
  view: "table",
  apply_filters: true,
};

let openPanel = null;
const mselSearch = {};
let prefsTimer = null;

const $ = (id) => document.getElementById(id);

async function jget(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(r.statusText + " " + url);
  return r.json();
}

const escapeHtml = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " K";
  return (n / 1048576).toFixed(2) + " M";
}

function fmtTime(epoch) {
  const d = new Date(epoch * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return p(d.getDate()) + "/" + p(d.getMonth() + 1) + " " +
         p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}

function flagHtml(code) {
  if (!code || code.length !== 2) return '<span class="cc-none">—</span>';
  const emoji = String.fromCodePoint(
    0x1F1E6 + code.charCodeAt(0) - 65,
    0x1F1E6 + code.charCodeAt(1) - 65);
  return `<img class="fl" src="https://flagcdn.com/20x15/${code.toLowerCase()}.png" alt="${escapeHtml(code)}" title="${escapeHtml(code)}" loading="lazy" onerror="this.outerHTML='${emoji}'">`;
}

function badgeClass(s) {
  if (s < 300) return "b2xx";
  if (s < 400) return "b3xx";
  if (s < 500) return "b4xx";
  return "b5xx";
}

function filterParams() {
  const p = new URLSearchParams();
  const f = state.filters;
  for (const k of ["host", "status", "method", "country"])
    if (f[k] && f[k].length) p.set(k, f[k].join(","));
  for (const k of ["ip", "q", "from", "to"])
    if (f[k]) p.set(k, f[k]);
  if (f.hide_uptime) p.set("exclude_ua", "Uptime-Kuma");
  if (f.hide_local) p.set("exclude_local", "true");
  return p.toString();
}

function params() {
  const p = new URLSearchParams(filterParams());
  if (state.filters.simplified) p.set("collapse", "true");
  p.set("sort", state.sort);
  p.set("order", state.order);
  p.set("page", state.page);
  p.set("size", state.size);
  return p.toString();
}

/* ---------- persistence (server side) ---------- */

async function loadPrefs() {
  try {
    const p = await jget("/api/prefs");
    state.filters.host = Array.isArray(p.host) ? p.host : [];
    state.filters.status = Array.isArray(p.status) ? p.status : [];
    state.filters.method = Array.isArray(p.method) ? p.method : [];
    state.filters.country = Array.isArray(p.country) ? p.country : [];
    state.filters.ip = p.ip || "";
    state.filters.q = p.q || "";
    state.filters.hide_uptime = !!p.hide_uptime;
    state.filters.hide_local = !!p.hide_local;
    state.filters.simplified = !!p.simplified;
    state.filters.from = p.from || "";
    state.filters.to = p.to || "";
    if (typeof p.apply_filters === "boolean") state.apply_filters = p.apply_filters;
    if (p.page_size) state.size = parseInt(p.page_size, 10) || 50;
    $("fIp").value = state.filters.ip;
    $("fQ").value = state.filters.q;
    $("fHideUptime").checked = state.filters.hide_uptime;
    $("fHideLocal").checked = state.filters.hide_local;
    $("btnSimplified").classList.toggle("active", state.filters.simplified);
    $("fFrom").value = state.filters.from;
    $("fTo").value = state.filters.to;
    $("pageSize").value = String(state.size);
  } catch (e) { /* prefs not available */ }
}

function savePrefs() {
  clearTimeout(prefsTimer);
  prefsTimer = setTimeout(async () => {
    try {
      await fetch("/api/prefs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...state.filters, page_size: state.size, apply_filters: state.apply_filters }),
      });
    } catch (e) { /* ignore */ }
  }, 400);
}

/* ---------- stats / filters ---------- */

const HEADERS = [
  ["ts", "Time"],
  ["host", "Host"],
  ["ip", "IP"],
  ["method", "Method"],
  ["path", "Path"],
  ["status", "Status"],
  ["country", "Country"],
  ["time", "Response time"],
  ["body", "Bytes"],
  ["ua", "User-Agent"],
];

function renderHead() {
  const tr = $("tableHead");
  const hide = state.filters.simplified ? new Set(["path", "time", "body"]) : new Set();
  const headers = HEADERS.filter(([key]) => !hide.has(key));
  tr.innerHTML = headers.map(([key, label]) => {
    const active = state.sort === key;
    const arrow = active ? (state.order === "desc" ? " ▼" : " ▲") : "";
    return `<th data-sort="${key}">${label}<span class="arrow">${active ? arrow : ""}</span></th>`;
  }).join("");
  tr.querySelectorAll("th").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (state.sort === key) state.order = state.order === "desc" ? "asc" : "desc";
      else { state.sort = key; state.order = "desc"; }
      state.page = 1;
      refresh();
    });
  });
}

function renderCards(stats) {
  const f = (k) => `value c${k[0]}`;
  const myHosts = (stats.my_hosts || []).map((h) => ({ value: h.value, count: h.count, mine: true }));
  let hosts;
  if (stats.cards_hosts && stats.cards_hosts.length) {
    hosts = stats.cards_hosts.map((h) => {
      const found = stats.hosts.find((x) => x.value === h);
      return { value: h, count: found ? found.count : 0 };
    });
  } else {
    hosts = stats.hosts.slice(0, 6);
  }
  const seen = new Set(hosts.map((h) => h.value));
  const chips = [...hosts, ...myHosts.filter((h) => !seen.has(h.value))].map((h) => {
    const sel = (state.filters.host || []).includes(h.value) ? " selected" : "";
    return `<span class="chip${sel}${h.mine ? " mine" : ""}" data-host="${escapeHtml(h.value)}" title="${h.mine ? "Your public IP" : ""}">${escapeHtml(h.value)} <span class="n">${h.count.toLocaleString("en-US")}</span></span>`;
  }).join("");
  const cards = `
    <div class="card big">
      <div class="label">Requests</div>
      <div class="value">${stats.total.toLocaleString("en-US")}</div>
      <div class="chips">${chips}</div>
    </div>
    <div class="card"><div class="label">2xx</div><div class="value ${f("2xx")}">${stats.by_status["2xx"].toLocaleString("en-US")}</div></div>
    <div class="card"><div class="label">3xx</div><div class="value ${f("3xx")}">${stats.by_status["3xx"].toLocaleString("en-US")}</div></div>
    <div class="card"><div class="label">4xx</div><div class="value ${f("4xx")}">${stats.by_status["4xx"].toLocaleString("en-US")}</div></div>
    <div class="card"><div class="label">5xx</div><div class="value ${f("5xx")}">${stats.by_status["5xx"].toLocaleString("en-US")}</div></div>`;
  $("cards").innerHTML = cards;
  $("cards").querySelectorAll(".chip").forEach((c) => {
    c.addEventListener("click", () => {
      const v = c.dataset.host;
      const sel = state.filters.host || [];
      const i = sel.indexOf(v);
      if (i === -1) sel.push(v);
      else sel.splice(i, 1);
      state.filters.host = sel;
      state.page = 1;
      savePrefs();
      refresh();
    });
  });

  const nFiles = stats.files.length;
  const scanned = stats.last_scan ? new Date(stats.last_scan * 1000).toLocaleTimeString("en-US") : "—";
  $("scanInfo").textContent = `files: ${nFiles} · last scan: ${scanned}`;
}

function statusOptions(stats) {
  const buckets = [
    ["2xx", "2xx", stats.by_status["2xx"]],
    ["3xx", "3xx", stats.by_status["3xx"]],
    ["4xx", "4xx", stats.by_status["4xx"]],
    ["5xx", "5xx", stats.by_status["5xx"]],
  ].filter(([, , n]) => n > 0)
    .map(([v, label, n]) => ({ value: v, count: n }));
  return buckets.concat(stats.statuses.map((s) => ({ value: String(s.value), count: s.count })));
}

const MSEL_DEFS = [
  ["host", "Host"],
  ["status", "Status"],
  ["method", "Method"],
  ["country", "Country"],
];

function statusLabel(v) {
  let num = v;
  if (v.length === 3 && v[1] === "x") num = parseInt(v[0], 10) * 100;
  else num = parseInt(v, 10);
  const cls = badgeClass(isNaN(num) ? 599 : num);
  return `<span class="badge ${cls}">${escapeHtml(v)}</span>`;
}

function mselSummary(key, sel) {
  if (!sel.length) return "All";
  const parts = sel.slice(0, 3);
  const rest = sel.length > 3 ? ` +${sel.length - 3}` : "";
  return parts.join(", ") + rest;
}

function optionShown(key, v) {
  if (key === "status") return statusLabel(v);
  if (key === "country") return `${escapeHtml(v)} ${flagHtml(v)}`;
  return escapeHtml(v);
}

function renderMulti(stats) {
  const open = openPanel;
  const defs = state.filters.simplified
    ? MSEL_DEFS.filter(([key]) => key !== "method")
    : MSEL_DEFS;
  const data = {
    host: stats.hosts,
    status: statusOptions(stats),
    method: stats.methods,
    country: stats.countries,
  };
  $("filtersMulti").innerHTML = defs.map(([key, label]) => {
    const sel = state.filters[key] || [];
    const query = (mselSearch[key] || "").toLowerCase();
    const opts = data[key].filter((it) =>
      !query || String(it.value).toLowerCase().includes(query))
      .map((it) => {
        const v = String(it.value);
        const checked = sel.includes(v) ? " checked" : "";
        return `<label class="msel-item"><input type="checkbox" value="${escapeHtml(v)}"${checked}> ${optionShown(key, v)}</label>`;
      }).join("");
    const allChecked = sel.length === 0 ? " checked" : "";
    return `
      <div class="msel" data-key="${key}">
        <button class="msel-btn" type="button" data-btn="${key}">
          <span class="msel-title">${label}</span>
          <span class="msel-summary">${escapeHtml(mselSummary(key, sel))}</span>
          <span class="msel-caret">▾</span>
        </button>
        <div class="msel-panel" data-panel="${key}" ${open === key ? "" : "hidden"}>
          <input type="search" class="msel-search" placeholder="search…" value="${escapeHtml(mselSearch[key] || "")}" autocomplete="off">
          <label class="msel-item"><input type="checkbox" class="msel-all"${allChecked}> All</label>
          <div class="msel-list">${opts}</div>
        </div>
      </div>`;
  }).join("");

  $("filtersMulti").querySelectorAll(".msel-btn").forEach((b) => {
    b.addEventListener("click", () => {
      const key = b.dataset.btn;
      const panel = document.querySelector(`[data-panel="${key}"]`);
      const wasOpen = !panel.hidden;
      document.querySelectorAll(".msel-panel").forEach((p) => { p.hidden = true; });
      panel.hidden = wasOpen;
      openPanel = wasOpen ? null : key;
    });
  });

  $("filtersMulti").querySelectorAll(".msel-search").forEach((inp) => {
    inp.addEventListener("input", () => {
      const key = inp.closest(".msel").dataset.key;
      mselSearch[key] = inp.value;
      const q = inp.value.toLowerCase();
      const list = inp.closest(".msel-panel").querySelector(".msel-list");
      list.querySelectorAll(".msel-item").forEach((it) => {
        it.hidden = !it.textContent.toLowerCase().includes(q);
      });
    });
  });

  $("filtersMulti").querySelectorAll("input[type=checkbox]").forEach((cb) => {
    cb.addEventListener("change", () => {
      const key = cb.closest(".msel").dataset.key;
      const panel = cb.closest(".msel-panel");
      if (cb.classList.contains("msel-all")) {
        state.filters[key] = cb.checked ? [] : state.filters[key];
        panel.querySelectorAll(".msel-item input:not(.msel-all)").forEach((o) => { o.checked = cb.checked; });
      } else {
        const sel = state.filters[key] || [];
        const v = cb.value;
        const i = sel.indexOf(v);
        if (cb.checked && i === -1) sel.push(v);
        if (!cb.checked && i !== -1) sel.splice(i, 1);
        state.filters[key] = sel;
        panel.querySelector(".msel-all").checked = sel.length === 0;
      }
      state.page = 1;
      savePrefs();
      refresh();
    });
  });
}

/* ---------- table / entries ---------- */

function renderRows(data) {
  const body = $("logBody");
  const items = data.items;
  $("empty").hidden = items.length > 0;
  if (!items.length) {
    body.innerHTML = "";
    return;
  }
  body.innerHTML = items.map((r) => {
    const cells = [
      `<td>${fmtTime(r.epoch)}${r.count > 1 ? ` <span class="badge cnt">×${r.count}</span>` : ""}</td>`,
      `<td class="host" title="${escapeHtml(r.host)}">${escapeHtml(r.host)}</td>`,
      `<td>${escapeHtml(r.ip)}</td>`,
      `<td>${escapeHtml(r.method)}</td>`,
    ];
    if (!state.filters.simplified) {
      cells.push(`<td class="path" title="${escapeHtml(r.path)}">${escapeHtml(r.path)}</td>`);
    }
    cells.push(`<td><span class="badge ${badgeClass(r.status)}">${r.status}</span></td>`);
    cells.push(`<td title="${escapeHtml(r.country || "unknown")}" class="cc">${r.country ? `<span class="cc-code">${escapeHtml(r.country)}</span>${flagHtml(r.country)}` : '<span class="cc-none">—</span>'}</td>`);
    if (!state.filters.simplified) {
      cells.push(`<td>${r.time_ms != null ? r.time_ms + " ms" : "—"}</td>`);
      cells.push(`<td>${fmtBytes(r.bytes != null ? r.bytes : r.body)}</td>`);
    }
    cells.push(`<td class="ua" title="${escapeHtml(r.ua)}">${escapeHtml(r.ua)}</td>`);
    return `<tr>${cells.join("")}</tr>`;
  }).join("");

  const totalPages = Math.max(1, Math.ceil(data.total / data.size));
  $("pageInfo").textContent =
    `Page ${data.page} of ${totalPages} · ${data.total.toLocaleString("en-US")} results`;
  $("prevPage").disabled = data.page <= 1;
  $("nextPage").disabled = data.page >= totalPages;
}

/* ---------- load ---------- */

let inFlight = 0;

const isMobile = () => window.matchMedia("(max-width: 760px)").matches;

function applyView() {
  const isMap = state.view === "map";
  $("viewTable").hidden = isMap;
  $("viewMap").hidden = !isMap;
  $("btnMode").hidden = !isMap;
  $("btnApply").hidden = !isMap;
  $("btnView").textContent = isMap ? "Table" : "Map";
  if (isMap) {
    if (window.App && window.App.map) window.App.map.show();
  } else {
    refresh();
  }
}

async function refresh() {
  if (inFlight > 1) return;
  inFlight++;
  try {
    const stats = await jget("/api/stats");
    renderCards(stats);
    if (state.view === "map") return;
    renderMulti(stats);
    const entries = await jget("/api/entries?" + params());
    renderHead();
    renderRows(entries);
  } catch (e) {
    console.error(e);
  } finally {
    inFlight--;
  }
}

/* ---------- events ---------- */

function bindFilters() {
  let t = null;
  const bindText = (id, key) => {
    $(id).addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(() => {
        state.filters[key] = $(id).value.trim();
        state.page = 1;
        savePrefs();
        refresh();
      }, 400);
    });
  };
  bindText("fIp", "ip");
  bindText("fQ", "q");

  const bindDate = (id, key) => {
    $(id).addEventListener("change", () => {
      state.filters[key] = $(id).value;
      state.page = 1;
      savePrefs();
      refresh();
    });
  };
  bindDate("fFrom", "from");
  bindDate("fTo", "to");

  $("fHideUptime").addEventListener("change", () => {
    state.filters.hide_uptime = $("fHideUptime").checked;
    state.page = 1;
    savePrefs();
    refresh();
  });

  $("fHideLocal").addEventListener("change", () => {
    state.filters.hide_local = $("fHideLocal").checked;
    state.page = 1;
    savePrefs();
    refresh();
  });

  $("btnSimplified").addEventListener("click", () => {
    state.filters.simplified = !state.filters.simplified;
    if (state.filters.simplified && state.filters.method.length) {
      state.filters.method = [];
      mselSearch.method = "";
    }
    $("btnSimplified").classList.toggle("active", state.filters.simplified);
    state.page = 1;
    savePrefs();
    refresh();
    if (state.view === "map" && window.App && window.App.map) window.App.map.reload(true);
  });

  $("btnClear").addEventListener("click", () => {
    state.filters.host = [];
    state.filters.status = [];
    state.filters.method = [];
    state.filters.country = [];
    for (const k of ["ip", "q", "from", "to"]) state.filters[k] = "";
    state.filters.hide_uptime = false;
    state.filters.hide_local = false;
    state.filters.simplified = false;
    for (const id of ["fIp", "fQ", "fFrom", "fTo"]) $(id).value = "";
    $("fHideUptime").checked = false;
    $("fHideLocal").checked = false;
    $("btnSimplified").classList.remove("active");
    state.page = 1;
    savePrefs();
    refresh();
  });

  $("btnReload").addEventListener("click", async () => {
    try { await fetch("/api/reload", { method: "POST" }); } catch (e) {}
    refresh();
  });

  $("live").addEventListener("change", () => { state.live = $("live").checked; });

  $("btnView").addEventListener("click", () => {
    state.view = state.view === "table" ? "map" : "table";
    applyView();
  });

  $("pageSize").addEventListener("change", () => {
    state.size = parseInt($("pageSize").value, 10);
    state.page = 1;
    savePrefs();
    refresh();
  });

  $("prevPage").addEventListener("click", () => { if (state.page > 1) { state.page--; refresh(); } });
  $("nextPage").addEventListener("click", () => { state.page++; refresh(); });

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".msel")) {
      openPanel = null;
      document.querySelectorAll(".msel-panel").forEach((p) => { p.hidden = true; });
    }
  });
}

renderHead();
bindFilters();
if (isMobile()) state.view = "map";
loadPrefs().then(() => applyView());
setInterval(() => { if (state.live && document.visibilityState === "visible") refresh(); }, 5000);

window.App = { state, filterParams, params, jget, $, refresh };
