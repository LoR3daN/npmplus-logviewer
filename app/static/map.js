"use strict";
(function () {
  const App = window.App;
  const $ = App.$;

  const C_ACCENT = "#4f8cff";
  const C_HOME = "#2ecc71";

  let HOME = null;
  let HOME_NAME = "";
  let inited = false;
  let map2d = null;
  let globe = null;
  let mode = "2d";
  let history = [];
  let rateHits = [];
  let lastEpoch = 0;
  let reloading = false;

  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const flagImg = (c) =>
    `<img class="fi" src="https://flagcdn.com/20x15/${c.toLowerCase()}.png" alt="${c}" title="${c}" onerror="this.remove()">`;
  const fmtTime = (ms) => {
    const d = new Date(ms);
    const p = (x) => String(x).padStart(2, "0");
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  };

  /* ---------------- 2D ---------------- */

  function arcPoints(lat1, lon1, lat2, lon2) {
    const dLon = ((lon2 - lon1 + 540) % 360) - 180;
    const p0 = [lat1, lon1], p2 = [lat2, lon1 + dLon];
    const mx = (p0[0] + p2[0]) / 2, my = (p0[1] + p2[1]) / 2;
    const dLat = p2[0] - p0[0], dLn = p2[1] - p0[1];
    const len = Math.hypot(dLat, dLn) || 1;
    const lift = Math.min(len * 0.2, 16);
    const ctrl = [mx - (dLn / len) * lift, my + (dLat / len) * lift];
    const pts = [], n = 60;
    for (let i = 0; i <= n; i++) {
      const t = i / n, a = (1 - t) * (1 - t), b = 2 * (1 - t) * t, c = t * t;
      pts.push([a * p0[0] + b * ctrl[0] + c * p2[0], a * p0[1] + b * ctrl[1] + c * p2[1]]);
    }
    return pts;
  }

  function solidLine(pts) {
    const line = L.polyline([pts[0], pts[0]], { color: C_ACCENT, weight: 3, opacity: 0.95, className: "beam" }).addTo(map2d);
    const t0 = performance.now(), dur = 1100;
    (function step() {
      let t = (performance.now() - t0) / dur; if (t > 1) t = 1;
      const i = Math.max(1, Math.floor(t * (pts.length - 1)));
      line.setLatLngs(pts.slice(0, i + 1));
      if (t < 1) requestAnimationFrame(step);
      else {
        setTimeout(() => { line.setStyle({ opacity: 0 }); setTimeout(() => map2d.removeLayer(line), 500); }, 800);
      }
    })();
  }

  const pulseIcon = L.divIcon({
    className: "pulse-wrap", html: '<span class="pulse-dot"></span>',
    iconSize: [13, 13], iconAnchor: [6, 6],
  });

  function animate2D(item) {
    const pulse = L.marker([item.lat, item.lon], { icon: pulseIcon }).addTo(map2d);
    if (HOME) solidLine(arcPoints(item.lat, item.lon, HOME[0], HOME[1]));
    setTimeout(() => map2d.removeLayer(pulse), 2100);
  }

  /* ---------------- 3D ---------------- */

  let arcs3d = [];
  let rings3d = [];

  function animate3D(item) {
    if (HOME) {
      arcs3d.push({ startLat: item.lat, startLng: item.lon, endLat: HOME[0], endLng: HOME[1] });
      if (arcs3d.length > 26) arcs3d.shift();
      globe.arcsData(arcs3d);
    }
    rings3d.push({ lat: item.lat, lng: item.lon, color: C_ACCENT });
    if (rings3d.length > 34) rings3d.splice(1, rings3d.length - 34);
    globe.ringsData(rings3d);
  }

  /* ---------------- init ---------------- */

  function init() {
    if (inited) return;
    inited = true;

    map2d = L.map("map2d", {
      zoomControl: false, minZoom: 2, maxZoom: 14,
      maxBounds: L.latLngBounds([[-58, -180], [84, 180]]),
      maxBoundsViscosity: 0.8,
    }).setView(HOME ? [HOME[0], HOME[1]] : [25, 0], 3);
    map2d.attributionControl.setPosition("bottomleft");
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
      attribution: '&copy; OpenStreetMap &copy; CARTO', maxZoom: 19,
    }).addTo(map2d);

    if (HOME) {
      const homeIcon = L.divIcon({
        className: "home-wrap",
        html: '<span class="home-ring"></span><span class="home-dot">⌂</span>',
        iconSize: [20, 20], iconAnchor: [10, 10],
      });
      L.marker(HOME, { icon: homeIcon, zIndexOffset: 1000 })
        .addTo(map2d).bindTooltip(HOME_NAME + " · home", { direction: "top", offset: [0, -12] });
    }

    globe = Globe()
      .backgroundColor("rgba(8,13,24,0)")
      .showAtmosphere(true).atmosphereColor("#2a5ec9").atmosphereAltitude(0.2)
      .globeImageUrl("https://cdn.jsdelivr.net/npm/three-globe@2.31.0/example/img/earth-dark.jpg")
      (document.getElementById("globe3d"));
    const gmat = globe.globeMaterial();
    gmat.color = new THREE.Color(0x0e1a30);
    gmat.emissive = new THREE.Color(0x050a16);
    gmat.emissiveIntensity = 0.15;
    globe.globeMaterial(gmat);
    globe.pointOfView({ lat: HOME ? HOME[0] : 25, lng: HOME ? HOME[1] : 0, altitude: 2.3 }, 0);
    globe.controls().autoRotate = true;
    globe.controls().autoRotateSpeed = 0.4;
    globe.arcsData([])
      .arcStartLat(d => d.startLat).arcStartLng(d => d.startLng)
      .arcEndLat(d => d.endLat).arcEndLng(d => d.endLng)
      .arcColor(() => C_ACCENT).arcAltitude(0.25).arcStroke(0.5)
      .arcDashLength(0.4).arcDashGap(0.5).arcDashAnimateTime(1600);
    globe.ringsData(rings3d)
      .ringLat(d => d.lat).ringLng(d => d.lng).ringColor(d => d.color)
      .ringMaxRadius(3).ringPropagationSpeed(1.3).ringRepeatPeriod(900);
    globe.pointsData(HOME ? [{ lat: HOME[0], lng: HOME[1] }] : [])
      .pointLat(d => d.lat).pointLng(d => d.lng)
      .pointColor(() => C_HOME).pointRadius(0.3);

    $("btnMode").addEventListener("click", () => {
      mode = mode === "2d" ? "3d" : "2d";
      document.getElementById("map2d").style.display = mode === "2d" ? "block" : "none";
      document.getElementById("globe3d").style.display = mode === "3d" ? "block" : "none";
      $("btnMode").textContent = mode === "2d" ? "Switch to 3D" : "Switch to 2D";
      if (mode === "2d") map2d.invalidateSize();
      else setTimeout(() => window.dispatchEvent(new Event("resize")), 50);
    });

    const btnApply = $("btnApply");
    btnApply.textContent = App.state.apply_filters ? "Apply table filters: ON" : "Apply table filters: OFF";
    btnApply.classList.toggle("active", App.state.apply_filters);
    btnApply.addEventListener("click", () => {
      App.state.apply_filters = !App.state.apply_filters;
      btnApply.textContent = App.state.apply_filters ? "Apply table filters: ON" : "Apply table filters: OFF";
      btnApply.classList.toggle("active", App.state.apply_filters);
      fullReload(true);
    });

    setInterval(poll, 4000);
  }

  /* ---------------- data ---------------- */

  function liveUrl(extra) {
    const p = new URLSearchParams();
    if (App.state.apply_filters) {
      const fp = new URLSearchParams(App.filterParams());
      for (const [k, v] of fp) p.set(k, v);
    }
    if (App.state.filters.simplified) p.set("collapse", "true");
    for (const [k, v] of Object.entries(extra || {})) if (v != null) p.set(k, v);
    return "/api/live?" + p.toString();
  }

  function pushItem(it) {
    it._ts = it.epoch * 1000;
    history.push(it);
    rateHits.push(it._ts);
    if (history.length > 1200) history.shift();
    if (rateHits.length > 400) rateHits.shift();
    if (mode === "2d") animate2D(it); else animate3D(it);
    addFeed(it);
    showStatus(it);
    renderStats();
  }

  /* ---------------- feed / status / stats ---------------- */

  function addFeed(it) {
    const el = document.createElement("div");
    el.className = "feed-item";
    el.innerHTML = `<span class="t">${fmtTime(it._ts)}</span>` +
      `<span class="h">${esc(it.host)}</span><span class="p">${esc(it.path)}</span>` +
      `<span class="cc">${esc(it.cc)} ${flagImg(it.cc)}</span>`;
    const list = $("mfeedList");
    list.prepend(el);
    while (list.children.length > 16) list.removeChild(list.lastChild);
  }

  function showStatus(it) {
    const status = $("status");
    status.innerHTML = `${esc(it.method)} ${esc(it.path)} · <span class="host">${esc(it.host)}</span>` +
      (HOME ? `<span class="cc"> · ${esc(it.cc)} ${flagImg(it.cc)} → ${esc(HOME_NAME)}</span>` : "");
    status.classList.add("show");
    clearTimeout(status._t);
    status._t = setTimeout(() => status.classList.remove("show"), 2100);
  }

  const STATUS_COLORS = { "2xx": "#2ecc71", "3xx": "#f1c40f", "4xx": "#e67e22", "5xx": "#e74c3c" };
  const statusBucket = (s) => s < 300 ? "2xx" : s < 400 ? "3xx" : s < 500 ? "4xx" : "5xx";

  function renderStats() {
    const byCc = {}, byIp = {}, byStatus = { "2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0 };
    history.forEach((h) => {
      byCc[h.cc] = (byCc[h.cc] || 0) + 1;
      byIp[h.ip] = (byIp[h.ip] || 0) + 1;
      byStatus[statusBucket(h.status)]++;
    });
    const total = history.length;
    const top = (obj, n) => Object.entries(obj).sort((a, b) => b[1] - a[1]).slice(0, n);

    $("mTotal").textContent = total.toLocaleString();
    $("mCountries").textContent = Object.keys(byCc).length;
    $("mIps").textContent = Object.keys(byIp).length;

    const sb = $("mstatusbar");
    const stEl = { "2xx": $("m2"), "3xx": $("m3"), "4xx": $("m4"), "5xx": $("m5") };
    sb.innerHTML = Object.keys(STATUS_COLORS).map((k) =>
      `<i style="width:${total ? Math.round(byStatus[k] / total * 100) : 0}%;background:${STATUS_COLORS[k]}"></i>`).join("");
    for (const k in stEl) stEl[k].textContent = `${k} ${byStatus[k]}`;

    const nowMs = Date.now();
    const buckets = new Array(20).fill(0);
    rateHits.forEach((t) => { const idx = Math.floor((nowMs - t) / 3000); if (idx >= 0 && idx < 20) buckets[19 - idx]++; });
    const max = Math.max(1, ...buckets);
    $("mspark").innerHTML =
      `<polyline fill="none" stroke="#4f8cff" stroke-width="2" points="` +
      buckets.map((v, i) => `${(i / 19) * 100},${38 - (v / max) * 34}`).join(" ") +
      `"></polyline>` +
      `<polyline fill="rgba(79,140,255,.15)" stroke="none" points="0,38 ${buckets.map((v, i) => `${(i / 19) * 100},${38 - (v / max) * 34}`).join(" ")} 100,38"></polyline>`;

    const WINDOW = 60 * 1000;
    let hitsIn = 0;
    for (let i = rateHits.length - 1; i >= 0; i--) {
      if (nowMs - rateHits[i] <= WINDOW) hitsIn++;
      else break;
    }
    let spanMs = WINDOW;
    if (rateHits.length && nowMs - rateHits[0] < WINDOW) spanMs = Math.max(1, nowMs - rateHits[0]);
    $("mRate").textContent = Math.round(hitsIn * 3600000 / spanMs).toLocaleString() + " req/h";

    const barHtml = (list, maxN) => list.map(([k, n]) =>
      `<div class="row"><span class="l">${k}</span><span class="v">${n}</span></div>` +
      `<div class="bar"><i style="width:${Math.round(n / maxN * 100)}%;background:var(--accent)"></i></div>`).join("");

    const origins = top(byCc, 5);
    $("mOrigins").innerHTML =
      barHtml(origins.map(([cc, n]) => [flagImg(cc) + " " + cc, n]), origins[0] ? origins[0][1] : 1);
    const ipts = top(byIp, 5);
    $("mIpTop").innerHTML = barHtml(ipts, ipts[0] ? ipts[0][1] : 1);
  }

  /* ---------------- load / poll ---------------- */

  async function fullReload(resetStats) {
    if (reloading) return;
    reloading = true;
    try {
      if (resetStats) {
        history = [];
        rateHits = [];
      }
      lastEpoch = Date.now() / 1000;
      $("mfeedList").innerHTML = "";
      $("status").classList.remove("show");
      renderStats();
    } finally {
      reloading = false;
    }
  }

  async function poll() {
    if (reloading) return;
    if (App.state.view !== "map" || !App.state.live) return;
    try {
      const data = await App.jget(liveUrl({ after: lastEpoch || 1, limit: 100 }));
      const items = data.items || [];
      for (const it of items) pushItem(it);
      if (items.length) lastEpoch = items[items.length - 1].epoch;
    } catch (e) { /* server not reachable yet */ }
  }

  function show() {
    App.jget("/api/home").then((h) => {
      if (h && typeof h.lat === "number" && typeof h.lon === "number") {
        HOME = [h.lat, h.lon];
        HOME_NAME = h.city && h.cc ? `${h.city}, ${h.cc}` : (h.cc || "home");
      }
    }).catch(() => {}).finally(() => {
      init();
      if (mode === "2d") map2d.invalidateSize();
      else setTimeout(() => window.dispatchEvent(new Event("resize")), 50);
      fullReload(false);
    });
  }

  function reload(resetStats) {
    fullReload(resetStats);
  }

  window.App.map = { show, reload };
})();
