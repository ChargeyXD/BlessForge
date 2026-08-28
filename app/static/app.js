/* ==========================================================================
   BlessForge front end — rebuilt 2026-08-29 from design/BlessForge.dc.html

   Three files, no build step, one IIFE. Screens are rendered into #canvas by
   JavaScript rather than sitting in index.html: there are fourteen of them,
   most are driven entirely by API data, and keeping them as markup would mean
   a 2,000-line shell where nothing is legible.

   Two rules worth keeping:
     * Everything interpolated goes through esc(). Log lines, mod names, crash
       reports and config files are all attacker-adjacent text.
     * A job owns its stream; a view onto it is disposable. "Run in background"
       closes the view, not the EventSource.
   ========================================================================== */
(() => {
"use strict";

/* --- core ---------------------------------------------------------------- */

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const num = (n) => (n == null ? "—" : Number(n).toLocaleString());
const mb  = (b) => (b == null ? "—" : b >= 1073741824 ? (b / 1073741824).toFixed(1) + " GB"
                                                      : (b / 1048576).toFixed(1) + " MB");
const pct = (n) => (n == null ? "—" : Math.round(n) + "%");

/** Relative time, because "3 days ago" reads faster than a timestamp. */
function ago(iso) {
  if (!iso) return "";
  const t = typeof iso === "number" ? iso * 1000 : Date.parse(iso);
  if (!t || Number.isNaN(t)) return "";
  const s = (Date.now() - t) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 2592000) return `${Math.floor(s / 86400)}d ago`;
  return new Date(t).toLocaleDateString();
}

async function api(path, { method = "GET", body, raw } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j && j.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch { /* a proxy error page, not JSON */ }
    throw new Error(detail);
  }
  return raw ? res : res.json();
}

/* --- state --------------------------------------------------------------- */

const state = {
  view: "discover",       // discover | activity | instance
  tab: "situation",
  fleet: [],
  inst: null,             // { id, server, manifest, stats }
  mods: [],
  modView: [],
  health: null,
  ai: { available: false },
  systemsOpen: false,
  roll: null,             // the current roulette hand
};

/* --- toasts -------------------------------------------------------------- */

const TOAST_GLYPH = { ok: "✓", err: "✕", warn: "▲", info: "ⓘ" };

function toast(message, tone = "info", ms = 5200) {
  const el = document.createElement("div");
  el.className = `toast ${tone}`;
  el.innerHTML = `<span class="glyph">${TOAST_GLYPH[tone] || "ⓘ"}</span>
                  <span style="flex:1">${esc(message)}</span>`;
  const close = document.createElement("button");
  close.className = "btn sm ghost"; close.textContent = "×";
  close.onclick = () => el.remove();
  el.appendChild(close);
  $("#toasts").appendChild(el);
  if (ms) setTimeout(() => el.remove(), ms);
  return el;
}

/* --- sheets (the one modal primitive) ------------------------------------ */

function sheet({ title, sub, body, size = "md", actions = [], onClose, top = false }) {
  const veil = document.createElement("div");
  veil.className = "veil" + (top ? " top" : "");
  veil.innerHTML = `
    <div class="sheet ${size}" role="dialog" aria-modal="true" aria-label="${esc(title || "Dialog")}">
      ${title ? `<header>
        <h2>${esc(title)}</h2>
        ${sub ? `<span class="topbar-sub">${esc(sub)}</span>` : ""}
        <span class="spacer"></span>
        <button class="btn sm ghost close" aria-label="Close">×</button>
      </header>` : ""}
      <div class="content"></div>
      ${actions.length ? `<footer></footer>` : ""}
    </div>`;

  const content = $(".content", veil);
  if (typeof body === "string") content.innerHTML = body;
  else if (body) content.appendChild(body);

  const api_ = {
    el: veil, content,
    close() { veil.remove(); document.removeEventListener("keydown", onKey); if (onClose) onClose(); },
    error(msg) {
      let box = $(".sheet-error", content);
      if (!box) {
        box = document.createElement("div");
        box.className = "note crit sheet-error";
        box.style.marginTop = "12px";
        content.appendChild(box);
      }
      box.innerHTML = `<span class="glyph">✕</span><span>${esc(msg)}</span>`;
      box.scrollIntoView({ block: "nearest" });
    },
    buttons: [],
  };

  const foot = $("footer", veil);
  actions.forEach((a) => {
    const b = document.createElement("button");
    b.className = "btn " + (a.cls || "");
    b.textContent = a.label;
    b.onclick = () => a.onClick(api_, b);
    if (a.right) foot.appendChild(Object.assign(document.createElement("span"), { className: "spacer" }));
    foot.appendChild(b);
    api_.buttons.push(b);
  });

  const closeBtn = $(".close", veil);
  if (closeBtn) closeBtn.onclick = () => api_.close();
  veil.addEventListener("mousedown", (e) => { if (e.target === veil) api_.close(); });
  const onKey = (e) => { if (e.key === "Escape") api_.close(); };
  document.addEventListener("keydown", onKey);

  $("#overlays").appendChild(veil);
  const focusable = $("input, button:not(.close), textarea", content);
  if (focusable) setTimeout(() => focusable.focus(), 40);
  return api_;
}

/** A confirm that can demand the name be typed, for the irreversible ones. */
function confirmSheet({ title, message, detail, confirmLabel = "Confirm",
                        cancelLabel = "Keep it", danger = false, typeToConfirm }) {
  return new Promise((resolve) => {
    const s = sheet({
      title, size: "sm",
      body: `
        <p class="prose">${esc(message)}</p>
        ${detail ? `<pre class="evidence" style="margin-top:12px">${esc(detail)}</pre>` : ""}
        ${typeToConfirm ? `
          <div class="label" style="margin-top:16px">Type “${esc(typeToConfirm)}” to confirm</div>
          <input class="field mono" id="typeGate" autocomplete="off" spellcheck="false"
                 style="margin-top:6px" aria-label="Type the name to confirm">` : ""}`,
      actions: [
        { label: cancelLabel, onClick: (m) => { m.close(); resolve(false); } },
        { label: confirmLabel, cls: danger ? "danger" : "primary",
          onClick: (m) => { m.close(); resolve(true); } },
      ],
      onClose: () => resolve(false),
    });
    if (typeToConfirm) {
      const go = s.buttons[1];
      const gate = $("#typeGate", s.content);
      go.disabled = true;
      gate.addEventListener("input", () => {
        go.disabled = gate.value.trim() !== typeToConfirm;
      });
    }
  });
}

/* --- routing -------------------------------------------------------------
   Two levels: a top-level view (discover / activity / a server), and, when a
   server is open, a tab within it. The tab strip is rebuilt per view rather
   than hidden, because Discover and a server share no sections at all. */

const INSTANCE_TABS = [
  { key: "situation", label: "Situation" },
  { key: "diagnose",  label: "Diagnose" },
  { key: "mods",      label: "Mods",    badge: () => state.mods.length || "" },
  { key: "tune",      label: "Tune" },
  { key: "console",   label: "Console" },
  { key: "configs",   label: "Configs" },
];

const DISCOVER_TABS = [
  { key: "roulette",  label: "Mod Roulette" },
  { key: "catalogue", label: "Catalogue" },
  { key: "import",    label: "Import an export" },
];

const RENDER = {};        // key -> async (host) => void, filled in below

function tabsFor(view) {
  if (view === "instance") return INSTANCE_TABS;
  if (view === "discover") return DISCOVER_TABS;
  return [];
}

function paintTabs() {
  const list = tabsFor(state.view);
  $("#tabs").innerHTML = list.map((t) => `
    <button class="tab" role="tab" data-tab="${t.key}"
            aria-selected="${t.key === state.tab}">
      ${esc(t.label)}
      ${t.badge && t.badge() ? `<span class="badge">${esc(String(t.badge()))}</span>` : ""}
    </button>`).join("");
  $$("#tabs .tab").forEach((b) => { b.onclick = () => go(state.view, b.dataset.tab); });
}

let renderToken = 0;

/** Navigate. Every screen renders into #canvas and owns nothing outside it. */
async function go(view, tab, opts = {}) {
  if (view === "instance" && !state.inst && !opts.id) view = "discover";
  const list = tabsFor(view);
  if (!tab || !list.some((t) => t.key === tab)) tab = list.length ? list[0].key : "";

  // Leaving a screen must dispose whatever it started -- a console stream, a
  // scroll observer. Screens register cleanups here.
  disposeScreen();

  state.view = view;
  state.tab = tab;
  $$(".nav-item[data-view]").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
  $$(".srv").forEach((c) => c.classList.toggle("on",
    view === "instance" && state.inst && c.dataset.id === state.inst.id));
  paintTabs();
  paintContext();

  const host = $("#canvas");
  host.scrollTop = 0;
  const mine = ++renderToken;
  const fn = RENDER[`${view}:${tab}`] || RENDER[view];
  if (!fn) { host.innerHTML = ""; return; }
  host.innerHTML = `<div class="loading"><span class="spin"></span> Loading…</div>`;
  try {
    const frag = await fn();
    if (mine !== renderToken) return;      // a newer navigation already won
    host.innerHTML = "";
    if (frag) {
      host.appendChild(frag);
      // Only now is the screen in the document, so anything that looks
      // itself up by id has to wait until here.
      if (frag.__mount) frag.__mount();
    }
  } catch (e) {
    if (mine !== renderToken) return;
    host.innerHTML = `<div class="screen"><div class="note crit">
      <span class="glyph">✕</span><span>${esc(e.message)}</span></div></div>`;
  }
}

let screenCleanups = [];
const onLeave = (fn) => screenCleanups.push(fn);
function disposeScreen() {
  screenCleanups.forEach((fn) => { try { fn(); } catch { /* already gone */ } });
  screenCleanups = [];
}

/** Build a screen element from HTML, so render functions stay declarative. */
function screen(html, cls = "") {
  const el = document.createElement("div");
  el.className = "screen " + cls;
  el.innerHTML = html;
  return el;
}

function paintContext() {
  const t = $("#ctxTitle"), s = $("#ctxSub");
  if (state.view === "instance" && state.inst) {
    const i = state.inst;
    t.textContent = i.server.server_name;
    const bits = [i.manifest.loader, i.manifest.minecraft, ":" + i.server.server_port]
      .filter(Boolean);
    s.textContent = bits.join(" · ");
  } else if (state.view === "discover") {
    t.textContent = "Discover";
    s.textContent = "build a pack, roll one, or import your own";
  } else {
    t.textContent = "Activity";
    s.textContent = "installs, scans and reviews";
  }
}

/* --- the fleet spine ----------------------------------------------------- */

const STATE_META = {
  running:    { label: "RUNNING",  tone: "ok",   live: true },
  crashed:    { label: "CRASHED",  tone: "crit" },
  orphan:     { label: "ORPHAN",   tone: "warn" },
  incomplete: { label: "UNFINISHED", tone: "warn" },
  stopped:    { label: "STOPPED",  tone: "" },
};

function srvCard(s) {
  const meta = STATE_META[s.state] || STATE_META.stopped;
  const pack = s.pack ? [s.pack.name, s.pack.version].filter(Boolean).join(" · ")
                      : (s.managed ? "managed" : "unmanaged");
  const gauge = (v, label) => `
    <span class="gauge ${v > 85 ? "crit" : v > 60 ? "hot" : ""}" title="${label} ${pct(v)}">
      <i style="width:${Math.max(0, Math.min(100, v || 0))}%"></i></span>`;

  return `
    <button class="srv" data-id="${esc(s.server_id)}" data-state="${esc(s.state)}">
      <div class="srv-top">
        <span class="srv-name">${esc(s.name)}</span>
        <span class="spacer"></span>
        <span class="pill ${meta.tone} ${meta.live ? "live" : ""}"><span class="dot"></span>${meta.label}</span>
      </div>
      <div class="srv-pack">${esc(pack)}</div>
      <div class="srv-meta">
        <span>${esc(s.loader || "—")}</span><span>${esc(s.minecraft || "—")}</span>
        <span>:${esc(s.port)}</span>
        ${s.state === "running" && s.players != null
          ? `<span class="spacer"></span><span>${esc(String(s.players))}${s.max_players ? "/" + esc(String(s.max_players)) : ""} online</span>`
          : ""}
      </div>
      ${s.state === "running"
        ? `<div class="srv-meta">${gauge(s.cpu, "CPU")}${gauge(s.mem, "Memory")}</div>`
        : s.state !== "stopped"
          ? `<div class="srv-note" style="color:var(--${meta.tone === "crit" ? "red-soft" : "amber"})">
               <span>${meta.tone === "crit" ? "✕" : "▲"}</span>
               <span>${esc(stateNote(s))}</span></div>`
          : ""}
    </button>`;
}

function stateNote(s) {
  if (s.state === "orphan") return "Crafty cannot open this server";
  if (s.state === "incomplete") return "Install never finished";
  if (s.state === "crashed") return "Crashed — see Diagnose";
  return "";
}

async function loadFleet() {
  const host = $("#fleet");
  try {
    const r = await api("/api/instances");
    state.fleet = r.items || [];
  } catch (e) {
    host.innerHTML = `<div class="note crit" style="margin:8px">
      <span class="glyph">✕</span><span>${esc(e.message)}</span></div>`;
    $("#fleetSummary").textContent = "";
    return;
  }

  const running = state.fleet.filter((s) => s.state === "running").length;
  const bad = state.fleet.filter((s) => ["crashed", "orphan", "incomplete"].includes(s.state)).length;
  $("#fleetSummary").textContent = state.fleet.length
    ? `${running}/${state.fleet.length} up${bad ? ` · ${bad} need you` : ""}` : "";

  host.innerHTML = state.fleet.length
    ? state.fleet.map(srvCard).join("")
    : `<div class="empty" style="padding:24px 12px">
         <div class="t">No servers yet</div>
         <div class="d">Roll one, or install a modpack from the catalogue.</div>
       </div>`;
  $$(".srv", host).forEach((c) => { c.onclick = () => openInstance(c.dataset.id); });
  $$(".srv", host).forEach((c) => c.classList.toggle("on",
    state.inst && c.dataset.id === state.inst.id));
}

async function openInstance(id) {
  try {
    const d = await api(`/api/instances/${id}`);
    state.inst = { id, server: d.server, manifest: d.manifest || {}, stats: d.stats || {} };
    state.mods = [];
    go("instance", "situation");
    loadFleet();
  } catch (e) {
    // Crafty answers 500 for a server whose files are gone and keeps doing so.
    // That is a state, not a crash, so it gets a screen rather than a toast.
    state.inst = null;
    state.view = "instance";
    paintTabs();
    $("#ctxTitle").textContent = "Unavailable";
    $("#ctxSub").textContent = id.slice(0, 8);
    $("#canvas").innerHTML = "";
    $("#canvas").appendChild(screen(`
      <div class="card">
        <div class="claim crit">
          <span class="glyph">✕</span>
          <div class="body">
            <div class="t">Crafty cannot open this server</div>
            <div class="why">${esc(e.message)}</div>
            <p class="prose" style="margin-top:10px">
              This normally means Crafty still holds a record for the server but its
              files are gone from disk. Crafty skips such servers when it starts and
              then fails every request about them until the record is removed.
            </p>
          </div>
        </div>
      </div>`));
  }
}

/* --- jobs -----------------------------------------------------------------
   A job owns its stream and outlives any view onto it. The drawer is one such
   view; Activity offers another. Closing either detaches, it does not cancel.

   The `end` frame is the only one that terminates a stream. A job also emits a
   final step whose status already reads `done`, and treating that as the end
   closed the connection one frame before the result arrived. */

const jobs = new Map();

const PHASES = ["Resolve", "Download", "Unpack", "Register", "Tune"];
const PHASE_RE = [
  [/resolv|manifest|prepar|analys|read|plan|pin/i, 0],
  [/download|fetch|server pack|catalog/i, 1],
  [/unpack|extract|upload|writ|copy|overrid/i, 2],
  [/regist|creat|crafty|instance/i, 3],
  [/tune|optimi|heap|flag|java|eula|port|final/i, 4],
];
const phaseOf = (step) => PHASE_RE.reduce((a, [re, i]) => (re.test(step || "") ? Math.max(a, i) : a), 0);

function job(id, title, onDone, { auto = false } = {}) {
  let e = jobs.get(id);
  if (e) { if (onDone) e.onDone = onDone; return e; }
  e = {
    id, title: title || "Task", onDone: onDone || null, auto,
    status: "pending", step: "Starting…", percent: 0, error: "", result: null,
    started: Date.now(), log: [], seen: new Map(), stream: "", views: new Set(), es: null,
  };
  jobs.set(id, e);
  connect(e);
  return e;
}

function connect(e) {
  const es = new EventSource(`/api/jobs/${e.id}/events`);
  e.es = es;
  es.onmessage = (ev) => {
    let d; try { d = JSON.parse(ev.data); } catch { return; }
    if (d.step) e.step = d.step;
    if (typeof d.percent === "number") e.percent = d.percent;
    if (d.status) e.status = d.status;
    if (d.server_name) e.serverName = d.server_name;
    if (d.server_id) e.serverId = d.server_id;

    if (d.event === "log" && d.message) pushLog(e, d.message, d.level || "info");
    if (d.event === "stream" && d.message) e.stream += d.message;
    if (d.event === "snapshot") {
      (d.log || []).forEach((l) => pushLog(e, l.message, l.level));
      if (d.stream) e.stream = d.stream;
    }
    e.views.forEach((v) => v.update());

    if (d.event === "end") {
      es.close(); e.es = null; e.finished = true;
      e.result = d.result || e.result;
      e.error = d.error || e.error;
      if (d.status === "done") toast(`${e.title} finished`, "ok");
      else if (d.status === "error") toast(`${e.title} failed`, "err");
      else if (d.status === "cancelled") toast(`${e.title} cancelled`, "warn");
      if (d.status === "done" && e.onDone && !e.ranDone) {
        e.ranDone = true;
        try { e.onDone(d); } catch (err) { console.error(err); }
      }
      e.views.forEach((v) => v.settle());
      refreshJobBadge();
      loadFleet();
    }
  };
  es.onerror = () => { es.close(); e.es = null; };
}

/** Deduplicated, but the repeat count is kept: "downloading" ×184 is one line. */
function pushLog(e, message, level = "info") {
  const key = level + "|" + message;
  const at = e.seen.get(key);
  if (at != null) { e.log[at].n += 1; return; }
  e.seen.set(key, e.log.length);
  e.log.push({ message, level, n: 1 });
}

const logHTML = (l) => `
  <div class="log-line ${l.level === "warn" ? "warn" : l.level === "error" ? "err" : ""}">
    <span class="lvl">${esc(l.level)}</span><span class="msg">${esc(l.message)}</span>
    ${l.n > 1 ? `<span class="dupe">×${l.n}</span>` : ""}
  </div>`;

/* --- the drawer ---------------------------------------------------------- */

function openDrawer(entry) {
  const host = $("#drawerHost");
  host.innerHTML = "";
  const el = document.createElement("aside");
  el.className = "drawer";
  el.innerHTML = `
    <div class="drawer-head">
      <div style="flex:1;min-width:0">
        <div class="label" id="dPhase">Phase 1 of 5</div>
        <div class="drawer-title" id="dTitle">${esc(entry.title)}</div>
        <div class="topbar-sub" id="dStep">${esc(entry.step)}</div>
      </div>
      <div class="mono" id="dClock" style="font-size:var(--fs-10);color:var(--text-5)">0s</div>
      <button class="btn sm ghost" id="dClose" aria-label="Detach this view">×</button>
    </div>
    <div class="drawer-bar"><div class="bar" id="dBarWrap"><i id="dBar"></i></div>
      <div class="phases" id="dPhases">${PHASES.map((p) => `<span>${p}</span>`).join("")}</div></div>
    <div class="drawer-stream hidden" id="dStreamWrap">
      <div class="label">Assistant output</div>
      <div class="log stream" id="dStream"></div>
    </div>
    <div class="drawer-log" id="dLog"></div>
    <div class="drawer-foot" id="dFoot"></div>`;
  host.appendChild(el);

  let tick = null;
  const view = {
    update() {
      $("#dStep", el).textContent = entry.step;
      const p = phaseOf(entry.step);
      $("#dPhase", el).textContent = `Phase ${p + 1} of ${PHASES.length}`;
      $$("#dPhases span", el).forEach((s, i) => {
        s.classList.toggle("done", i < p);
        s.classList.toggle("now", i === p);
      });
      const wrap = $("#dBarWrap", el);
      wrap.classList.toggle("indeterminate", !entry.percent);
      $("#dBar", el).style.width = (entry.percent || 0) + "%";

      const log = $("#dLog", el);
      log.innerHTML = entry.log.map(logHTML).join("");
      log.scrollTop = log.scrollHeight;

      if (entry.stream) {
        $("#dStreamWrap", el).classList.remove("hidden");
        const s = $("#dStream", el);
        const pinned = s.scrollHeight - s.scrollTop - s.clientHeight < 40;
        s.textContent = entry.stream;
        if (pinned) s.scrollTop = s.scrollHeight;
      }
    },
    settle() {
      if (tick) clearInterval(tick);
      drawerSummary(entry, el);
    },
  };

  tick = setInterval(() => {
    const s = Math.round((Date.now() - entry.started) / 1000);
    $("#dClock", el).textContent = s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : `${s}s`;
  }, 500);

  const detach = () => {
    entry.views.delete(view);
    if (tick) clearInterval(tick);
    host.innerHTML = "";
  };
  $("#dClose", el).onclick = detach;

  $("#dFoot", el).innerHTML = `
    <button class="btn sm danger" id="dCancel">Cancel</button>
    <span class="spacer"></span>
    <button class="btn sm" id="dBg">Run in background</button>`;
  $("#dBg", el).onclick = detach;
  $("#dCancel", el).onclick = async () => {
    const ok = await confirmSheet({
      title: "Cancel this task", danger: true, confirmLabel: "Cancel the task",
      message: `Stop “${entry.title}”? Anything already written to the instance stays where it is.`,
    });
    if (!ok) return;
    try {
      const r = await api(`/api/jobs/${entry.id}/cancel`, { method: "POST" });
      toast(r.cancelled ? "Cancellation requested" : "It had already finished", r.cancelled ? "ok" : "info");
    } catch (e) { toast(e.message, "err"); }
  };

  entry.views.add(view);
  view.update();
  if (entry.finished) view.settle();
  return view;
}

/** A finished job resolves into something the user dismisses, not a vanish:
    the warnings are frequently the point, and half of them predict the first
    boot failing. */
function drawerSummary(entry, el) {
  const r = entry.result || {};
  const notes = entry.log.filter((l) => l.level === "warn" || l.level === "error");
  const failed = entry.status === "error" || entry.status === "cancelled";
  const tone = failed ? "crit" : notes.length ? "warn" : "ok";
  const glyph = failed ? "✕" : notes.length ? "▲" : "✓";
  const title = failed
    ? (entry.status === "cancelled" ? "Cancelled" : "Failed")
    : notes.length ? `Finished with ${notes.length} warning${notes.length === 1 ? "" : "s"}`
                   : "Finished cleanly";

  const head = $(".drawer-head", el);
  head.innerHTML = `
    <span class="glyph-lg ${tone}">${glyph}</span>
    <div style="flex:1;min-width:0">
      <div class="drawer-title">${esc(title)}</div>
      <div class="topbar-sub">${esc(entry.title)}</div>
    </div>
    <button class="btn sm ghost" id="dClose2" aria-label="Dismiss">×</button>`;
  $("#dClose2", el).onclick = () => { entry.views.clear(); $("#drawerHost").innerHTML = ""; };

  const stats = [];
  if (r.mods_installed != null) stats.push(["mods", num(r.mods_installed)]);
  if (r.client_only_disabled) stats.push(["disabled", num(r.client_only_disabled.length)]);
  if (r.files_uploaded != null) stats.push(["files", num(r.files_uploaded)]);
  if (r.port) stats.push(["port", r.port]);

  $(".drawer-bar", el).outerHTML = stats.length
    ? `<div class="drawer-bar"><div class="stats">${stats.map(([k, v]) =>
        `<div class="stat"><div class="k">${esc(k)}</div><div class="v">${esc(String(v))}</div></div>`).join("")}</div></div>`
    : "";

  const foot = $("#dFoot", el);
  foot.innerHTML = "";
  if (failed && entry.error) {
    foot.insertAdjacentHTML("beforebegin",
      `<div style="padding:0 14px 12px"><div class="note crit">
         <span class="glyph">✕</span><span>${esc(entry.error)}</span></div></div>`);
  }
  const roll = r.roll;
  if (roll && roll.export_available) {
    foot.innerHTML += `<a class="btn sm accent" href="/api/roulette/export/${esc(roll.roll_id)}"
                          download>Download the pack</a>`;
  }
  if (r.server_id) {
    foot.innerHTML += `<button class="btn sm primary" id="dOpen">Open the server</button>`;
  }
  foot.innerHTML += `<span class="spacer"></span><button class="btn sm" id="dDone">Dismiss</button>`;
  const open = $("#dOpen", el);
  if (open) open.onclick = () => { $("#drawerHost").innerHTML = ""; openInstance(r.server_id); };
  $("#dDone", el).onclick = () => { entry.views.clear(); $("#drawerHost").innerHTML = ""; };
}

/** The single entry point every long operation uses. */
function follow(jobId, title, onDone, opts) {
  const e = job(jobId, title, onDone, opts);
  openDrawer(e);
  refreshJobBadge();
  return e;
}

async function refreshJobBadge() {
  try {
    const r = await api("/api/jobs");
    const live = (r.items || []).filter((j) => !["done", "error", "cancelled"].includes(j.status));
    $("#jobBadge").classList.toggle("hidden", !live.length);
    $("#jobBadgeN").textContent = live.length;
  } catch { /* the systems panel already reports an unreachable backend */ }
}

/* --- systems -------------------------------------------------------------
   The health panel. Every row is a fact from /api/health or /api/ai/status;
   nothing here is decorative, and the one row that can be acted on (a model
   the endpoint does not have) offers the action. */

async function loadSystems() {
  let h, ai;
  try { h = await api("/api/health"); } catch (e) {
    $("#systemsPill").className = "pill crit";
    $("#systemsN").textContent = "offline";
    $("#systems").innerHTML = `<div class="note crit"><span class="glyph">✕</span>
      <span>The BlessForge backend is not answering (${esc(e.message)}).</span></div>`;
    return;
  }
  try { ai = await api("/api/ai/status"); } catch { ai = { available: false, reason: "unreachable" }; }
  state.health = h; state.ai = ai;

  const c = h.checks || {};
  const rows = [];
  rows.push(c.crafty?.ok
    ? { k: "Crafty Controller", v: `reachable · ${c.crafty.servers} server${c.crafty.servers === 1 ? "" : "s"}`
        + (c.crafty.latency_ms != null ? ` · ${c.crafty.latency_ms} ms` : ""), s: "ok" }
    : { k: "Crafty Controller", v: c.crafty?.error || "unreachable", s: "crit" });

  rows.push(c.curseforge?.ok
    ? { k: "CurseForge API key", v: "accepted", s: "ok" }
    : { k: "CurseForge API key", v: c.curseforge?.error || "not set", s: "crit" });

  rows.push({ k: "Modrinth", v: c.modrinth?.ok ? "enabled · anonymous" : (c.modrinth?.error || "disabled"),
              s: c.modrinth?.ok ? "ok" : "" });

  rows.push(c.storage?.ok
    ? { k: "Data directory", v: `writable${c.storage.free_gb != null ? ` · ${c.storage.free_gb} GB free` : ""}`, s: "ok" }
    : { k: "Data directory", v: c.storage?.error || "not writable", s: "crit" });

  rows.push(ai.available
    ? { k: "AI assistant", v: `${ai.model} · ${hostOf(ai.url)}`, s: "ok" }
    : /is not installed/.test(ai.reason || "")
      ? { k: "AI assistant", v: ai.reason, s: "warn", fix: "Pull the model", act: pullModel }
      : { k: "AI assistant", v: ai.reason || "unavailable", s: "warn" });

  const bad = rows.filter((r) => r.s === "crit").length;
  const warn = rows.filter((r) => r.s === "warn").length;
  $("#systemsPill").className = "pill " + (bad ? "crit" : warn ? "warn" : "ok");
  $("#systemsN").textContent = bad ? `${bad} broken` : warn ? `${warn} to sort` : "all good";

  $("#systems").innerHTML = rows.map((r) => `
    <div class="sysrow ${r.s}">
      <span class="dot"></span>
      <div style="flex:1;min-width:0">
        <div class="k">${esc(r.k)}</div>
        <div class="v">${esc(r.v)}</div>
      </div>
      ${r.fix ? `<button class="btn sm" data-fix="${esc(r.k)}">${esc(r.fix)}</button>` : ""}
    </div>`).join("");
  rows.filter((r) => r.act).forEach((r) => {
    const b = $(`[data-fix="${CSS.escape(r.k)}"]`, $("#systems"));
    if (b) b.onclick = r.act;
  });
}

const hostOf = (url) => { try { return new URL(url).host; } catch { return url || ""; } };

async function pullModel() {
  const ok = await confirmSheet({
    title: "Fetch the assistant's model",
    message: `The endpoint is reachable but does not have the model BlessForge expects. `
           + `Ask it to download qwen3:4b-instruct now? It is about 2.5 GB, and it is `
           + `fetched onto the Ollama host, not onto this machine.`,
    confirmLabel: "Pull the model",
  });
  if (!ok) return;
  try {
    const r = await api("/api/ai/pull", { method: "POST", body: {} });
    follow(r.job_id, "Pulling the model", () => loadSystems());
  } catch (e) { toast(e.message, "err"); }
}

/* --- command palette ------------------------------------------------------
   Every server, every section of the open server, and the handful of global
   actions, in one list. It is the only navigation that reaches a section of a
   server you are not currently looking at. */

function openPalette() {
  const items = [];
  state.fleet.forEach((s) => items.push({
    group: "Servers", label: s.name,
    hint: `${s.loader || "—"} ${s.minecraft || ""} · ${STATE_META[s.state]?.label.toLowerCase() || ""}`,
    run: () => openInstance(s.server_id),
  }));
  if (state.inst) INSTANCE_TABS.forEach((t) => items.push({
    group: state.inst.server.server_name, label: t.label,
    hint: "section", run: () => go("instance", t.key),
  }));
  DISCOVER_TABS.forEach((t) => items.push({
    group: "Discover", label: t.label, hint: "section", run: () => go("discover", t.key),
  }));
  items.push({ group: "Go", label: "Activity", hint: "running and recent work",
               run: () => go("activity") });

  const s = sheet({
    size: "pal", top: true,
    body: `<div class="pal-bar">
             <span class="mono pal-caret">›</span>
             <input class="field pal-input" id="palQ" autocomplete="off"
                    placeholder="Jump to a server or a section…">
             <button class="kbd" id="palEsc" title="Close">ESC</button>
           </div>
           <div id="palList" class="pal-list"></div>`,
  });

  const q = $("#palQ", s.content), list = $("#palList", s.content);
  $("#palEsc", s.content).onclick = () => s.close();
  let cursor = 0, shown = items;

  const paint = () => {
    const term = q.value.trim().toLowerCase();
    shown = term
      ? items.filter((i) => (i.label + " " + i.group + " " + i.hint).toLowerCase().includes(term))
      : items;
    cursor = Math.min(cursor, Math.max(0, shown.length - 1));
    if (!shown.length) {
      list.innerHTML = `<div class="empty"><div class="t">Nothing matches</div></div>`;
      return;
    }
    let group = "";
    list.innerHTML = shown.map((i, n) => {
      const head = i.group !== group ? `<div class="label pal-group">${esc(i.group)}</div>` : "";
      group = i.group;
      return head + `<button class="pal-item ${n === cursor ? "on" : ""}" data-n="${n}">
        <span>${esc(i.label)}</span><span class="spacer"></span>
        <span class="topbar-sub">${esc(i.hint)}</span></button>`;
    }).join("");
    $$(".pal-item", list).forEach((b) => {
      b.onclick = () => { s.close(); shown[Number(b.dataset.n)].run(); };
    });
    const on = $(".pal-item.on", list);
    if (on) on.scrollIntoView({ block: "nearest" });
  };

  q.addEventListener("input", () => { cursor = 0; paint(); });
  q.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); cursor = Math.min(cursor + 1, shown.length - 1); paint(); }
    if (e.key === "ArrowUp")   { e.preventDefault(); cursor = Math.max(cursor - 1, 0); paint(); }
    if (e.key === "Enter" && shown[cursor]) { e.preventDefault(); s.close(); shown[cursor].run(); }
  });
  paint();
}

/* --- screens: placeholders until each is built --------------------------- */

const soon = (name) => () => screen(`
  <div class="card">
    <div class="empty">
      <div class="t">${esc(name)}</div>
      <div class="d">This screen is next in the rebuild. Its data is already
        served by the API — only the view is missing.</div>
    </div>
  </div>`);

RENDER["activity"]            = soon("Activity");
RENDER["instance:situation"]  = soon("Situation");
RENDER["instance:diagnose"]   = soon("Diagnose");
RENDER["instance:mods"]       = soon("Mods");
RENDER["instance:tune"]       = soon("Tune");
RENDER["instance:console"]    = soon("Console");
RENDER["instance:configs"]    = soon("Configs");
RENDER["discover:catalogue"]  = soon("Catalogue");
RENDER["discover:import"]     = soon("Import an export");

/* --- shell wiring -------------------------------------------------------- */

$$(".nav-item[data-view]").forEach((b) => { b.onclick = () => go(b.dataset.view); });
$("#paletteBtn").onclick = openPalette;
$("#newServerBtn").onclick = () => go("discover", "catalogue");
$("#drawerBtn").onclick = () => {
  const live = [...jobs.values()].filter((j) => !j.finished);
  const pick = live[live.length - 1] || [...jobs.values()].pop();
  if (pick) openDrawer(pick);
  else toast("Nothing is running", "info");
};
$("#systemsToggle").onclick = () => {
  state.systemsOpen = !state.systemsOpen;
  $("#systems").classList.toggle("hidden", !state.systemsOpen);
  $("#systemsToggle").setAttribute("aria-expanded", String(state.systemsOpen));
};

document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if (!$(".sheet.pal")) openPalette();
  }
});

/* Exposed for dev/ui-tests, which drive the real DOM rather than a mock and
   need a few entry points that have no clickable path of their own. Nothing
   in the app reads this. */
window.__bf = {
  get state() { return state; },
  go, openInstance, loadFleet, loadSystems, follow, toast, sheet, confirmSheet,
  openPalette, RENDER,
};

/* --- Mod Roulette ---------------------------------------------------------
   The constraints live on the right and the outcome on the left, because the
   outcome is what you look at and the constraints are what you fiddle with.

   Every roll is dealt on the server: the pool is three thousand mods and the
   determinism that makes a seed worth sharing lives in one implementation,
   not two. This screen sends constraints and renders what comes back. */

const R = {
  meta: null,
  c: null,             // constraints
  seed: "",
  hand: null,
  summary: null,
  pool: null,
  dropped: [],
  holds: new Set(),
  spinning: false,
};

const FLAG_META = {
  HEAVY:     { c: "amber",  g: "▲", t: "HEAVY" },
  CHAOS:     { c: "crit",   g: "✕", t: "UNMAINTAINED" },
  CLIENT:    { c: "amber",  g: "◑", t: "CLIENT-SIDE" },
  "CLIENT?": { c: "info",   g: "◔", t: "MAYBE CLIENT" },
};

RENDER["discover:roulette"] = async () => {
  if (!R.meta) {
    R.meta = await api("/api/roulette/meta");
    R.c = JSON.parse(JSON.stringify(R.meta.defaults));
    R.seed = R.meta.seed;
  }
  const el = screen(rouletteHTML(), "split wide");
  el.style.gridTemplateColumns = "minmax(0,1fr) 320px";
  el.__mount = () => { bindRoulette(el); refreshPool(); };
  return el;
};

function rouletteHTML() {
  return `
    <div style="min-width:0">
      <div class="card" style="margin-bottom:12px">
        <div style="display:flex;align-items:flex-start;gap:14px">
          <img src="/static/img/lucky-block.png" alt="" width="58" height="58"
               style="flex:0 0 58px;image-rendering:pixelated">
          <div style="flex:1;min-width:0">
            <h2 style="font-size:var(--fs-19);font-weight:700">Mod Roulette</h2>
            <p class="prose" style="margin-top:6px;max-width:64ch">
              Let the machine build the pack. Set the constraints, pull the lever, and
              BlessForge assembles a server out of mods nobody chose. Each pull mints a
              seed, and re-entering that seed with the same constraints deals the same
              hand. Nothing is written until you accept it.
            </p>
          </div>
        </div>

        <div class="seedbar">
          <div style="flex:1;min-width:0">
            <div class="label">Seed</div>
            <input class="field mono" id="rSeed" maxlength="12" spellcheck="false"
                   autocomplete="off" aria-label="Roll seed" style="margin-top:4px">
            <div class="hint" id="rSeedHint" style="margin-top:4px">pull deals a new one</div>
          </div>
          <button class="btn sm" id="rNewSeed" title="Mint a fresh seed">⚄ New</button>
          <button class="btn accent lg" id="rPull" style="min-width:132px">
            <span id="rPullLabel">PULL</span></button>
        </div>
      </div>

      <div id="rOut"></div>
    </div>

    <aside style="min-width:0">
      <div class="card" id="rPanel">
        <div class="label">Constraints</div>
        <div id="rSegs" style="margin-top:10px"></div>
        <div id="rSliders" style="margin-top:16px"></div>

        <div class="label" style="margin-top:18px">Categories · click to cycle</div>
        <div class="cats" id="rCats" style="margin-top:8px"></div>
        <div class="hint mono" style="margin-top:6px;font-size:var(--fs-9)">
          neutral → <span style="color:var(--green)">+ preferred</span> →
          <span style="color:var(--red-soft)">− banned</span></div>

        <div id="rToggles" style="margin-top:16px"></div>

        <div class="pool" id="rPool"></div>
      </div>
    </aside>`;
}

function bindRoulette(root) {
  const seed = $("#rSeed", root);
  seed.value = R.seed;
  seed.addEventListener("input", () => {
    R.seed = seed.value.toUpperCase().slice(0, 12);
    seed.value = R.seed;
    $("#rSeedHint", root).textContent = "typed · pull to deal it";
  });
  $("#rNewSeed", root).onclick = async () => {
    const m = await api("/api/roulette/meta");
    R.seed = m.seed; seed.value = m.seed;
    $("#rSeedHint", root).textContent = "pull deals a new one";
  };
  $("#rPull", root).onclick = pull;
  paintControls(root);
  paintRoll(root);
}

/** Constraint controls. Rebuilt wholesale on change: this panel is small and
    a diffing scheme here would cost more than it saves. */
function paintControls(root) {
  const c = R.c;

  const segs = [
    { key: "minecraft", label: "Minecraft version", opts: ["1.20.1", "1.21.1", "1.21.4"] },
    { key: "loader", label: "Mod loader", opts: R.meta.loaders },
    { key: "source", label: "Source", opts: R.meta.sources },
  ];
  $("#rSegs", root).innerHTML = segs.map((s) => `
    <div style="margin-bottom:10px">
      <div class="label">${esc(s.label)}</div>
      <div class="seg" role="group" aria-label="${esc(s.label)}">
        ${s.opts.map((o) => `<button data-seg="${esc(s.key)}" data-val="${esc(o)}"
            class="${String(c[s.key]).toLowerCase() === String(o).toLowerCase() ? "on" : ""}">${esc(o)}</button>`).join("")}
      </div>
    </div>`).join("");

  const sliders = [
    { key: "count", label: "Target mod count", min: 5, max: 300, step: 5,
      shown: String(c.count),
      note: c.count > 200 ? "Above 200 on this host, expect a long first boot."
                          : "Comfortable for 8 threads and a 4 GB heap." },
    { key: "intensity", label: "Intensity", min: 1, max: 5, step: 1,
      shown: R.meta.intensity[c.intensity - 1],
      note: c.intensity > 3 ? "Unmaintained mods enter the pool. Boot at your own risk."
                            : "Sticks to maintained mods with a server-side code path." },
    { key: "quality", label: "Quality floor", min: 0, max: 50, step: 5,
      shown: c.quality === 0 ? "none" : c.quality + "M downloads",
      note: "Drops anything below this download count — a blunt but effective proxy for “someone still maintains it”." },
  ];
  $("#rSliders", root).innerHTML = sliders.map((s) => `
    <div style="margin-bottom:14px">
      <div style="display:flex;align-items:baseline;gap:8px">
        <span class="label">${esc(s.label)}</span><span class="spacer"></span>
        <span class="mono" style="font-size:var(--fs-12);color:${
          s.key === "intensity" && c.intensity > 3 ? "var(--red-soft)" : "var(--amber)"};font-weight:700">${esc(s.shown)}</span>
      </div>
      <input type="range" class="range" data-slider="${esc(s.key)}"
             min="${s.min}" max="${s.max}" step="${s.step}" value="${c[s.key]}"
             aria-label="${esc(s.label)}">
      <div class="hint" style="margin-top:2px;font-size:var(--fs-9)">${esc(s.note)}</div>
    </div>`).join("");

  $("#rCats", root).innerHTML = R.meta.categories.map((cat) => {
    const st = c.categories[cat.key] || 0;
    return `<button class="cat s${st}" data-cat="${esc(cat.key)}"
              title="${st === 1 ? "Preferred — weighted up" : st === 2 ? "Banned — excluded entirely" : "Neutral"}"
              style="--cat:${esc(cat.color)}">
        <span class="g">${st === 1 ? "+" : st === 2 ? "−" : esc(cat.glyph)}</span>
        <span>${esc(cat.title)}</span>
      </button>`;
  }).join("");

  const toggles = [
    { k: "deps", t: "Resolve dependencies automatically",
      d: "Anything the rolled mods need comes with them, and does not count against the hand." },
    { k: "client", t: "Allow client-side mods",
      d: "Off by default. Their authors say a server cannot run them, and some crash it on boot." },
    { k: "conflict", t: "Allow unmaintained mods",
      d: "The interesting failures live here. Needs intensity 4 or above to have any effect." },
    { k: "cap", t: "Keep single mods under 30 MB",
      d: "Stops one roll pulling three dimension mods at once." },
  ];
  $("#rToggles", root).innerHTML = toggles.map((t) => `
    <label class="tog">
      <span class="switch"><input type="checkbox" data-tog="${esc(t.k)}"
        ${c.toggles[t.k] ? "checked" : ""}><span class="track"></span></span>
      <span style="flex:1;min-width:0">
        <span style="font-size:var(--fs-11);color:var(--text-1)">${esc(t.t)}</span>
        <span class="hint" style="display:block;margin-top:2px;font-size:var(--fs-9)">${esc(t.d)}</span>
      </span>
    </label>`).join("");

  const changed = () => { R.hand = null; paintRoll(root); refreshPool(); };
  $$("[data-seg]", root).forEach((b) => b.onclick = () => {
    R.c[b.dataset.seg] = b.dataset.val; paintControls(root); changed();
  });
  $$("[data-slider]", root).forEach((i) => {
    i.oninput = () => {
      R.c[i.dataset.slider] = Number(i.value);
      // Repaint only the readouts while dragging; a full rebuild would drop
      // the slider's own focus mid-drag.
      const s = sliders.find((x) => x.key === i.dataset.slider);
      if (s) paintControls(root), $(`[data-slider="${i.dataset.slider}"]`, root).focus();
    };
    i.onchange = changed;
  });
  $$("[data-cat]", root).forEach((b) => b.onclick = () => {
    const k = b.dataset.cat;
    R.c.categories[k] = ((R.c.categories[k] || 0) + 1) % 3;
    paintControls(root); changed();
  });
  $$("[data-tog]", root).forEach((i) => i.onchange = () => {
    R.c.toggles[i.dataset.tog] = i.checked; paintControls(root); changed();
  });
}

let poolToken = 0;

/** How many mods survive the current constraints. Debounced, because it moves
    on every slider tick and each miss is a request. */
let poolTimer = null;
function refreshPool() {
  clearTimeout(poolTimer);
  const host = $("#rPool");
  if (host) host.innerHTML = `<div class="label">Pool after constraints</div>
    <div class="loading" style="padding:8px 0"><span class="spin"></span> counting…</div>`;
  poolTimer = setTimeout(async () => {
    const mine = ++poolToken;
    try {
      const start = await api("/api/roulette/pool", { method: "POST", body: R.c });
      const done = await waitForJob(start.job_id);
      if (mine !== poolToken) return;
      R.pool = done.result;
      paintPool();
    } catch (e) {
      if (mine !== poolToken) return;
      const h = $("#rPool");
      if (h) h.innerHTML = `<div class="note crit"><span class="glyph">✕</span>
        <span>${esc(e.message)}</span></div>`;
    }
  }, 400);
}

/** Poll a job to completion. Used where the work is short and a drawer would
    be more ceremony than the wait deserves. */
async function waitForJob(id, { onStep } = {}) {
  for (;;) {
    const j = await api(`/api/jobs/${id}`);
    if (onStep) onStep(j);
    if (j.status === "done") return j;
    if (j.status === "error") throw new Error(j.error || "the task failed");
    if (j.status === "cancelled") throw new Error("cancelled");
    await new Promise((r) => setTimeout(r, 900));
  }
}

function paintPool() {
  const host = $("#rPool");
  if (!host || !R.pool) return;
  const p = R.pool;
  const thin = p.eligible < Math.max(8, Math.floor(R.c.count / 4));
  host.innerHTML = `
    <div class="label">Pool after constraints</div>
    <div class="pool-n ${thin ? "thin" : ""}">${num(p.eligible)}<span> mods eligible</span></div>
    <div class="hint" style="margin-top:4px">${esc(p.note)}</div>
    <div class="pool-bars">
      ${R.meta.categories.map((c) => {
        const n = p.by_category[c.key] || 0;
        const w = p.eligible ? Math.round(100 * n / p.eligible) : 0;
        return `<div class="pool-bar" title="${esc(c.title)}: ${num(n)} eligible">
          <span class="mono">${esc(c.glyph)}</span>
          <span class="gauge"><i style="width:${w}%;background:${esc(c.color)}"></i></span>
          <span class="mono n">${num(n)}</span></div>`;
      }).join("")}
    </div>`;
}

async function pull() {
  if (R.spinning) return;
  R.spinning = true;
  const btn = $("#rPull"), label = $("#rPullLabel");
  if (btn) { btn.disabled = true; label.textContent = "ROLLING"; }
  paintRoll();
  try {
    const start = await api("/api/roulette/roll", {
      method: "POST",
      body: { seed: R.seed, constraints: R.c, holds: [...R.holds] },
    });
    const done = await waitForJob(start.job_id, {
      onStep: (j) => {
        const n = $("#rSpinStep");
        if (n && j.step) n.textContent = j.step;
      },
    });
    const res = done.result;
    R.seed = res.seed;
    R.hand = res.hand;
    R.summary = res.summary;
    R.pool = res.pool;
    R.dropped = res.dropped || [];
    const s = $("#rSeed");
    if (s) s.value = res.seed;
    const h = $("#rSeedHint");
    if (h) h.textContent = `dealt · ${res.hand.length} mods`;
  } catch (e) {
    toast(e.message, "err", 8000);
  } finally {
    R.spinning = false;
    if (btn) { btn.disabled = false; label.textContent = R.hand ? "PULL AGAIN" : "PULL"; }
    paintRoll();
    paintPool();
  }
}

function paintRoll(root = document) {
  const host = $("#rOut", root);
  if (!host) return;

  if (R.spinning) {
    host.innerHTML = `<div class="card"><div class="loading">
      <img src="/static/img/fox-loading.gif" alt="" width="52" style="image-rendering:pixelated">
      <span>Dealing a hand from ${R.pool ? num(R.pool.eligible) : "the"} eligible mods…<br>
        <span class="hint mono" id="rSpinStep">pinning a real build for every mod</span></span>
    </div></div>`;
    return;
  }

  if (!R.hand) {
    host.innerHTML = `<div class="card"><div class="empty">
      <div class="t">Nothing rolled yet</div>
      <div class="d">Set your constraints on the right, then pull. The pool counter
        updates as you change them, so you can see how much room the roll has
        before you spend one.</div>
    </div></div>`;
    return;
  }

  const s = R.summary;
  const tone = { good: "ok", warn: "warn", bad: "crit" }[s.odds.tone] || "info";

  host.innerHTML = `
    <div class="card odds ${tone}">
      <div style="display:flex;align-items:flex-start;gap:12px">
        <span class="glyph-lg ${tone}">${esc(s.odds.glyph)}</span>
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <h3 style="font-size:var(--fs-14);font-weight:700">${esc(s.odds.title)}</h3>
            <span class="pill ${tone}">${esc(s.odds.confidence)}</span>
          </div>
          <ul class="why" style="margin-top:8px">
            ${s.odds.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}
          </ul>
        </div>
      </div>
      <div class="stats" style="margin-top:14px">
        <div class="stat"><div class="k">mods in hand</div><div class="v">${num(s.mods)}</div></div>
        <div class="stat"><div class="k">est. jars</div><div class="v">${num(s.estimated_jars)}</div>
          <div class="sub">with dependencies</div></div>
        <div class="stat"><div class="k">download</div><div class="v">${esc(String(s.download_mb))}<span style="font-size:var(--fs-12)"> MB</span></div></div>
        <div class="stat"><div class="k">heap needed</div>
          <div class="v" style="color:${s.heap_gb > s.heap_ceiling_gb ? "var(--red-soft)" : "var(--green)"}">${esc(String(s.heap_gb))}<span style="font-size:var(--fs-12)"> GB</span></div>
          <div class="sub">ceiling ${esc(String(s.heap_ceiling_gb))} GB</div></div>
      </div>
      <div class="rollbar">
        <button class="btn primary" id="rAccept">Create the server from this hand</button>
        <button class="btn" id="rAgain">Pull again</button>
        <button class="btn ghost" id="rCopy">Copy seed</button>
        <span class="spacer"></span>
        <span class="hint mono">seed ${esc(R.seed)} · ${esc(s.intensity.toLowerCase())}</span>
      </div>
    </div>

    ${R.dropped.length ? `
      <div class="note info" style="margin-top:12px">
        <span class="glyph">ⓘ</span>
        <span><strong>${R.dropped.length}</strong> rolled ${R.dropped.length === 1 ? "mod was" : "mods were"} dropped before you saw them —
        ${esc(R.dropped.slice(0, 3).map((d) => `${d.name} (${d.reason})`).join("; "))}${R.dropped.length > 3 ? "…" : ""}</span>
      </div>` : ""}

    <div class="card flush" style="margin-top:12px">
      <div class="hand-head">
        <span class="label">The hand</span>
        <span class="spacer"></span>
        <span class="hint">hold a row to keep it through the next pull</span>
      </div>
      <div class="hand">${R.hand.map(handRow).join("")}</div>
      <div class="hand-foot hint">
        Dependencies are resolved on install, so the real jar count will be higher
        than the hand. Nothing is written until you accept it.
      </div>
    </div>`;

  $("#rAccept", host).onclick = acceptRoll;
  $("#rAgain", host).onclick = pull;
  $("#rCopy", host).onclick = async () => {
    try { await navigator.clipboard.writeText(R.seed); } catch { /* no clipboard on http */ }
    toast(`Seed ${R.seed} copied. Re-entering it with these constraints deals this exact hand.`, "ok");
  };
  $$("[data-hold]", host).forEach((b) => b.onclick = () => {
    const n = b.dataset.hold;
    if (R.holds.has(n)) R.holds.delete(n); else R.holds.add(n);
    paintRoll(root);
  });
  $$("[data-reroll]", host).forEach((b) => b.onclick = () => rerollOne(b.dataset.reroll));
}

function handRow(m) {
  const cat = R.meta.categories.find((c) => c.key === m.category) || { glyph: "⌗", color: "#9C9EA4", title: m.category };
  const held = R.holds.has(m.name);
  const fl = FLAG_META[m.flag];
  return `
    <div class="hand-row${held ? " held" : ""}">
      <button class="hold${held ? " on" : ""}" data-hold="${esc(m.name)}"
              title="${held ? "Kept through the next pull" : "Keep this through the next pull"}">${held ? "HELD" : "HOLD"}</button>
      <span class="cat-g" style="color:${esc(cat.color)}" title="${esc(cat.title)}">${esc(cat.glyph)}</span>
      <div style="flex:1;min-width:0">
        <div class="n">${esc(m.name)}</div>
        <div class="meta mono">${esc(cat.title)} · ${m.downloads ? (m.downloads / 1e6).toFixed(1) + "M" : "—"} · ${mb(m.size)}</div>
      </div>
      ${fl ? `<span class="pill ${fl.c}" title="${esc(Object.values(m.flag_why || {})[0] || "")}">${fl.g} ${fl.t}</span>` : ""}
      <button class="btn sm ghost" data-reroll="${esc(m.name)}" title="Reroll just this slot">⟲</button>
    </div>`;
}

async function rerollOne(name) {
  try {
    const res = await api("/api/roulette/reroll", {
      method: "POST",
      body: { seed: R.seed, constraints: R.c, hand: R.hand, mod: name },
    });
    R.hand = res.hand; R.summary = res.summary; R.seed = res.seed;
    paintRoll();
  } catch (e) { toast(e.message, "err"); }
}

/** Accepting a hand is the first thing here that writes anything, so it asks
    for the two facts an install needs and states plainly what will happen. */
async function acceptRoll() {
  const s = R.summary;
  const dlg = sheet({
    title: "Create the server from this hand",
    sub: `seed ${R.seed}`,
    size: "md",
    body: `
      <p class="prose">
        ${num(s.mods)} rolled mods, about ${num(s.estimated_jars)} jars once dependencies
        are resolved, ${esc(String(s.download_mb))} MB to download. BlessForge will create
        the server, install the pack, tune the heap, and keep a CurseForge export you can
        download or share.
      </p>
      ${s.heap_gb > s.heap_ceiling_gb ? `
        <div class="note warn" style="margin-top:12px"><span class="glyph">▲</span>
          <span>This hand wants about ${esc(String(s.heap_gb))} GB and this machine can
          safely give ${esc(String(s.heap_ceiling_gb))} GB. It will be installed with the
          smaller heap, and may struggle under load.</span></div>` : ""}
      <div style="display:flex;gap:12px;margin-top:16px">
        <div style="flex:1">
          <div class="label">Server name</div>
          <input class="field" id="rName" value="Roulette ${esc(R.seed)}" style="margin-top:4px">
        </div>
        <div style="width:120px">
          <div class="label">Port</div>
          <input class="field mono" id="rPort" type="number" value="25565" style="margin-top:4px">
        </div>
      </div>
      <label class="check" style="margin-top:14px">
        <input type="checkbox" id="rOpt" checked><span class="box"></span>
        <span>Tune the JVM for this host</span></label>`,
    actions: [
      { label: "Back", onClick: (m) => m.close() },
      { label: "Create the server", cls: "primary", onClick: async (m, b) => {
          b.disabled = true;
          try {
            const res = await api("/api/roulette/install", {
              method: "POST",
              body: {
                seed: R.seed, constraints: R.c, hand: R.hand,
                server_name: $("#rName", m.content).value.trim() || `Roulette ${R.seed}`,
                port: Number($("#rPort", m.content).value) || 25565,
                optimize: $("#rOpt", m.content).checked,
              },
            });
            m.close();
            follow(res.job_id, `Rolling ${R.seed}`, () => loadFleet());
          } catch (e) { b.disabled = false; m.error(e.message); }
        } },
    ],
  });
  return dlg;
}

/* --- boot -----------------------------------------------------------------
   Last on purpose: every RENDER above must be registered before the first
   go() runs, and relying on an await to yield long enough for the rest of
   the file to evaluate is not a guarantee worth depending on. */

(async function boot() {
  await loadSystems();
  await loadFleet();
  await refreshJobBadge();
  go("discover", "roulette");
  setInterval(loadSystems, 45000);
  setInterval(refreshJobBadge, 15000);
  setInterval(() => { if (state.view !== "instance") loadFleet(); }, 20000);
})();

})();
