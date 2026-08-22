/* BlessForge -- Single-Page Application (SPA) Frontend.
 * Ultra-Modern Glassmorphic Studio Interface with real-time SSE task streaming,
 * modpack installer, mod manager, config editor, diagnostics, AI assistant, and optimizer.
 */
(() => {
"use strict";

// --- DOM Helpers --------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const num = (n) => (n == null ? "—" : Number(n).toLocaleString());
const mb = (b) => (!b ? "" : b > 1048576 ? (b / 1048576).toFixed(1) + " MB"
                                         : (b / 1024).toFixed(0) + " KB");

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  if (kind === "err") el.setAttribute("role", "alert");

  let icon = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>`;
  if (kind === "ok") {
    icon = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>`;
  } else if (kind === "err") {
    icon = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg>`;
  }

  el.innerHTML = `${icon}<span>${esc(msg)}</span>`;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "err" ? 8500 : 4500);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const detail = (data && data.detail) || res.statusText || "request failed";
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

// Anything that can hold focus inside a dialog. Used both to trap Tab within
// the modal and to decide what gets focus when it opens.
const FOCUSABLE = [
  "a[href]", "button:not([disabled])", "input:not([disabled])",
  "select:not([disabled])", "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

let modalSeq = 0;

function modal({ title, body, actions = [], wide = false, narrow = false,
                 danger = false, onClose = null }) {
  const back = document.createElement("div");
  back.className = "modal-back";
  const titleId = `modalTitle${++modalSeq}`;
  const variant = [wide ? "wide" : "", narrow ? "narrow" : "", danger ? "danger" : ""]
    .filter(Boolean).join(" ");
  back.innerHTML = `
    <div class="modal ${variant}" role="dialog" aria-modal="true" aria-labelledby="${titleId}">
      <header>
        <h3 id="${titleId}">${esc(title)}</h3>
        <button class="close" data-x title="Close modal" aria-label="Close dialog">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg>
        </button>
      </header>
      <div class="content"></div>
      <div class="modal-error hidden" role="alert"></div>
      <div class="foot"></div>
    </div>`;
  const dialog = $(".modal", back);
  const content = $(".content", back);
  if (typeof body === "string") content.innerHTML = body; else content.appendChild(body);
  const foot = $(".foot", back);
  const errBox = $(".modal-error", back);

  // Focus goes back where it came from on close, so dismissing a dialog with
  // the keyboard does not dump the user at the top of the document.
  const opener = document.activeElement;
  let closed = false;

  const api2 = {
    el: back,
    content,
    foot,
    buttons: [],
    close: () => {
      if (closed) return;
      closed = true;
      back.remove();
      if (opener && opener.isConnected && typeof opener.focus === "function") {
        opener.focus();
      }
      if (onClose) onClose();
    },
    // A failure raised by a footer action used to exist only as a toast that
    // vanished after a few seconds, leaving the dialog looking like it had
    // simply done nothing. Errors now stay put until the next attempt.
    error: (msg) => {
      if (!msg) { errBox.classList.add("hidden"); errBox.textContent = ""; return; }
      errBox.classList.remove("hidden");
      errBox.textContent = String(msg);
    },
  };

  actions.forEach((a) => {
    const b = document.createElement("button");
    b.className = "btn " + (a.cls ? "btn-" + a.cls : "btn-secondary");
    b.textContent = a.label;
    b.onclick = () => a.onClick(api2, b);
    foot.appendChild(b);
    api2.buttons.push(b);
  });

  $("[data-x]", back).onclick = api2.close;
  back.addEventListener("click", (e) => { if (e.target === back) api2.close(); });

  back.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      api2.close();
      return;
    }
    if (e.key !== "Tab") return;
    const items = $$(FOCUSABLE, dialog).filter((el) => el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault(); first.focus();
    }
  });

  $("#modalRoot").appendChild(back);

  // Land focus inside the dialog so the trap has something to cycle and a
  // screen reader announces the dialog rather than the page behind it.
  const target = $$(FOCUSABLE, dialog).filter((el) => el.offsetParent !== null)[0];
  (target || dialog).focus({ preventScroll: true });

  return api2;
}

// Replacement for window.confirm(): stylable, escapable, and awaitable. Used
// for every destructive action so a stray Enter cannot delete a mod folder.
function confirmDialog({
  title = "Please confirm",
  message,
  detail = "",
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  danger = false,
}) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const m = modal({
      title,
      narrow: true,
      danger,
      body: `
        <p class="confirm-message">${esc(message)}</p>
        ${detail ? `<pre class="confirm-detail">${esc(detail)}</pre>` : ""}`,
      // Backdrop click, the X, and Escape all mean "no".
      onClose: () => done(false),
      actions: [
        { label: cancelLabel, onClick: (mm) => mm.close() },
        {
          label: confirmLabel,
          cls: danger ? "danger-solid" : "primary",
          onClick: (mm) => { done(true); mm.close(); },
        },
      ],
    });
    // Default focus to Cancel rather than the destructive button.
    if (m.buttons[0]) m.buttons[0].focus({ preventScroll: true });
  });
}

// Gauges tint at the same thresholds everywhere: ok below 65%, mid to 85%,
// high above it. Severity is never carried by hue alone -- the number stays.
const gaugeTier = (v) => (v > 85 ? "high" : v > 65 ? "mid" : "ok");

// The empty-state mark. Inline SVG so it renders with no network.
const FOX_MARK = `
  <svg class="fox-mark" viewBox="0 0 64 44" aria-hidden="true">
    <rect x="0" y="38" width="64" height="2" fill="#2a3024"></rect>
    <path d="M18 12l5 4h10l5-4-1 9 3 5-5 3-7 7-7-7-5-3 3-5z" fill="#3a2b20"></path>
    <path d="M28 33l-7-7 4-1h8l4 1z" fill="#4a4536"></path>
    <rect x="24" y="21" width="3" height="3" fill="#11130e"></rect><rect x="33" y="21" width="3" height="3" fill="#11130e"></rect>
    <rect x="8" y="34" width="3" height="3" fill="#2f3629"></rect><rect x="14" y="30" width="3" height="3" fill="#272d21"></rect>
    <rect x="49" y="34" width="3" height="3" fill="#2f3629"></rect><rect x="55" y="30" width="3" height="3" fill="#272d21"></rect>
  </svg>`;

function iconHTML(logo, name, cls = "mod-icon") {
  if (logo) return `<img class="${cls}" loading="lazy" src="${esc(logo)}" alt="">`;
  const letter = (String(name || "?").trim()[0] || "?").toUpperCase();
  return `<div class="${cls} placeholder">${esc(letter)}</div>`;
}

// --- State -------------------------------------------------------------

const state = {
  instances: [],
  inst: null,        // full detail of the active open instance
  mods: [],
  configs: [],
  cfgPath: null,
  cfgLoaded: "",       // pristine content, for unsaved-change detection
  cfgEditable: false,
  cfgMeta: "",
  packIndex: 0,
  modView: [],        // the filtered rows currently on screen
  modVirt: false,     // is the list windowed?
  modWindow: [0, 0],
  ai: { available: false },
  optObserver: null,
};

// --- Top-Level Navigation ---------------------------------------------

async function showView(name) {
  const leavingConfigs = $("#view-instance").classList.contains("active")
    && $("#tab-configs").classList.contains("active")
    && name !== "instance";
  if (leavingConfigs && !(await confirmLeaveConfig())) return;
  $$("#topNav button").forEach((b) => {
    const on = b.dataset.view === name;
    b.classList.toggle("active", on);
    if (on) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + name));
  if (name === "instances") loadInstances();
  if (name === "jobs") loadJobs();
  if (name === "browse" && !$("#packs").children.length) searchPacks(true);
  window.scrollTo(0, 0);
}

async function showTab(name) {
  const onConfigs = $("#tab-configs").classList.contains("active");
  if (onConfigs && name !== "configs" && !(await confirmLeaveConfig())) return;
  $$("#instTabs button").forEach((b) => {
    const on = b.dataset.tab === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  $$(".tabpane").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "mods") loadMods();
  if (name === "configs") loadConfigs();
  if (name === "optimize") loadOptimize();
  if (name === "pack") loadPackTab();
  if (name === "troubleshoot") prewarmAI();
}

// Expose navigation to window for any inline handlers
window.showView = showView;
window.showTab = showTab;

$$("#topNav button").forEach((b) => { b.onclick = () => showView(b.dataset.view); });
$("#homeBtn").onclick = () => showView("instances");
$("#backToList").onclick = () => showView("instances");
$("#gotoBrowse").onclick = () => showView("browse");
$$("#instTabs button").forEach((b) => { b.onclick = () => showTab(b.dataset.tab); });

let warmed = false;
function prewarmAI() {
  if (warmed || !state.ai.available) return;
  warmed = true;
  $("#aiHint").textContent = "Preloading local model into RAM…";
  api("/api/ai/warm", { method: "POST" })
    .then((r) => {
      $("#aiHint").textContent = r.warmed
        ? `${state.ai.model} active in RAM` : "";
    })
    .catch(() => { $("#aiHint").textContent = ""; });
}

// --- Health Check ------------------------------------------------------

async function checkHealth() {
  try {
    const h = await api("/api/health");
    const el = $("#health");
    const c = h.checks;
    el.className = "pill pill-health " + (h.ready ? "ok" : "err");
    el.title = h.ready ? "Crafty reachable" : (c.crafty.error || "Crafty unreachable");
    el.innerHTML = `
      <span class="pill-dot"></span>
      <span class="pill-text">${h.ready ? `Crafty · ${c.crafty.servers} Servers` : "Crafty Unreachable"}</span>
    `;
    // The setup banner is the first screen most people ever see, so it says
    // what is connected, what is not, and which variable fixes it.
    const rows = [
      { name: "Crafty Controller", ok: c.crafty.ok, why: c.crafty.ok
          ? `Connected · ${c.crafty.servers} servers` : c.crafty.error,
        vars: c.crafty.ok ? [] : ["CRAFTY_URL", "CRAFTY_TOKEN"] },
      { name: "CurseForge API", ok: c.curseforge.ok, why: c.curseforge.ok
          ? "Key accepted" : c.curseforge.error,
        vars: c.curseforge.ok ? [] : ["CURSEFORGE_API_KEY"] },
    ];
    // Storage only earns a row when it is broken: a read-only /data breaks
    // every install, and used to do it without saying anything at all.
    if (c.storage && !c.storage.ok) {
      rows.push({ name: "Storage", ok: false, why: c.storage.error, vars: [] });
    }
    const broken = rows.filter((x) => !x.ok);
    const banner = $("#setup");
    if (broken.length) {
      banner.classList.remove("hidden");
      banner.className = "banner banner-setup";
      banner.innerHTML = `
        <span class="setup-glyph"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l9 16H3z" fill="none" stroke="currentColor" stroke-width="2"></path><rect x="11" y="9" width="2" height="5" fill="currentColor"></rect><rect x="11" y="15.5" width="2" height="2" fill="currentColor"></rect></svg></span>
        <div class="setup-body">
          <div class="setup-title">${broken.length === 1
            ? "One thing left to sort out" : `${broken.length} things left to sort out`}</div>
          <div class="setup-sub">BlessForge talks to Crafty Controller and CurseForge. Set the
            variables below in the environment, then restart the container.</div>
          <div class="setup-list">
            ${rows.map((x) => `
              <div class="setup-item ${x.ok ? "ok" : "bad"}">
                <span class="dot"></span>
                <span class="n">${esc(x.name)}</span>
                <span class="why">${esc(x.why || "")}</span>
                ${x.vars.map((v) => `<code>${esc(v)}</code>`).join("")}
              </div>`).join("")}
          </div>
        </div>`;
    } else {
      banner.classList.add("hidden");
    }
  } catch {
    const el = $("#health");
    el.className = "pill pill-health err";
    el.innerHTML = `<span class="pill-dot"></span><span class="pill-text">Backend Offline</span>`;
  }
  try {
    const ai = await api("/api/ai/status");
    state.ai = ai;
    const p = $("#aiPill");
    p.classList.remove("hidden");
    p.className = "pill pill-ai " + (ai.available ? "ok" : "ghost");
    p.innerHTML = `
      <span class="pill-dot"></span>
      <span class="pill-text">${ai.available ? "AI Ready" : "AI Off"}</span>`;
    p.title = ai.available ? `Local model: ${ai.model}` : (ai.reason || "unavailable");
  } catch { /* AI is optional */ }
}

// --- Instances List ----------------------------------------------------

async function loadInstances() {
  const host = $("#instances");
  if (!host.children.length) {
    host.innerHTML = `<div class="skel skel-card"></div><div class="skel skel-card"></div><div class="skel skel-card"></div>`;
  }
  try {
    const r = await api("/api/instances");
    state.instances = r.items;

    if (!r.items.length) {
      host.innerHTML = `
        <div class="empty" style="grid-column:1/-1">
          ${FOX_MARK}
          <div class="t">No servers yet</div>
          <div class="d">Crafty has no instances registered. Install a pack from the catalogue,
            or bring one you exported yourself.</div>
          <div class="actions">
            <button class="btn btn-primary" id="emptyBrowseBtn">Browse Modpacks</button>
            <button class="btn btn-secondary" id="emptyImportBtn">Import an export</button>
          </div>
        </div>`;
      const btn = $("#emptyBrowseBtn");
      if (btn) btn.onclick = () => showView("browse");
      const imp = $("#emptyImportBtn");
      if (imp) imp.onclick = () => openImportModal();
      return;
    }

    host.innerHTML = r.items.map((i) => {
      const isRunning = Boolean(i.running);
      const memVal = i.mem != null ? Number(i.mem) : null;
      const cpuVal = i.cpu != null ? Number(i.cpu) : null;
      const sid = esc(i.server_id);
      // An imported pack usually carries no version string, so the subtitle has
      // to read correctly with the version half empty.
      const packLine = i.pack
        ? [i.pack.name || "Imported pack", i.pack.version || ""].filter(Boolean).join(" · ")
        : "Custom Minecraft Server";

      return `
        <article class="inst-card ${isRunning ? "running" : ""}" data-open="${sid}" data-sid="${sid}" tabindex="0">
          <div class="inst-card-head">
            <div style="min-width:0">
              <h3 class="inst-card-title" title="${esc(i.name)}">${esc(i.name)}</h3>
              <div class="inst-card-subtitle">${esc(packLine)}</div>
            </div>
            <span class="pill ${isRunning ? "pill-running" : ""}">
              <span class="pill-dot"></span>${isRunning ? "Running" : "Stopped"}
            </span>
          </div>

          <div class="inst-card-tags">
            ${i.managed ? '<span class="pill ok">BlessForge</span>'
                        : '<span class="pill ghost">unmanaged</span>'}
            ${i.loader ? `<span class="pill mono">${esc(i.loader)}</span>` : ""}
            ${i.minecraft ? `<span class="pill mono">${esc(i.minecraft)}</span>` : ""}
            <span class="pill mono" title="Server port">:${esc(i.port)}</span>
          </div>

          ${isRunning ? `
            <div class="inst-stats-bar">
              <div class="inst-stat-item">
                <div class="k">Players</div>
                <div class="v">${i.players != null ? esc(i.players) : "—"}</div>
              </div>
              ${memVal != null ? `
                <div class="inst-stat-item">
                  <div class="k">RAM</div>
                  <div class="v ${gaugeTier(memVal)}">${memVal}%</div>
                  <span class="mini-gauge"><span class="mini-gauge-fill ${gaugeTier(memVal)}" style="width:${Math.min(100, memVal)}%"></span></span>
                </div>` : ""}
              ${cpuVal != null ? `
                <div class="inst-stat-item">
                  <div class="k">CPU</div>
                  <div class="v ${gaugeTier(cpuVal)}">${cpuVal}%</div>
                  <span class="mini-gauge"><span class="mini-gauge-fill ${gaugeTier(cpuVal)}" style="width:${Math.min(100, cpuVal)}%"></span></span>
                </div>` : ""}
            </div>` : ""}

          <div class="inst-card-foot">
            <button class="btn btn-sm ${isRunning ? "btn-power-stop" : "btn-power-start"}"
                    data-card-power="${isRunning ? "stop_server" : "start_server"}"
                    data-sid="${sid}"
                    title="${isRunning ? "Gracefully stop server" : "Start server"}">
              ${isRunning ? "Stop" : "Start"}
            </button>
            ${isRunning ? `
              <button class="btn btn-sm btn-power-restart" data-card-power="restart_server"
                      data-sid="${sid}" title="Restart server">Restart</button>` : ""}
            <span class="inst-card-hint">Manage
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h13m-5-6l6 6-6 6" fill="none" stroke="currentColor" stroke-width="2.2"></path></svg>
            </span>
          </div>
        </article>`;
    }).join("");

    // The whole card opens the instance; the buttons inside it do not. Keyboard
    // users get the same target because the card is focusable.
    $$("#instances .inst-card").forEach((card) => {
      const open = (e) => {
        if (e.target.closest("button") || e.target.closest(".switch") || e.target.closest("input")) return;
        openInstance(card.dataset.open);
      };
      card.addEventListener("click", open);
      card.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(e); }
      });
    });

    $$("#instances [data-card-power]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        executeCardPower(btn);
      });
    });
  } catch (e) {
    toast(e.message, "err");
  }
}

async function executeCardPower(btn) {
  const sid = btn.dataset.sid;
  const act = btn.dataset.cardPower;
  btn.disabled = true;
  try {
    const r = await api(`/api/instances/${sid}/action/${act}`, { method: "POST" });
    const prep = r.prepared || {};
    if (prep.java) toast(`Java pinned to ${prep.java}`, "ok");
    if (prep.eula) toast("eula.txt validated for Crafty", "ok");
    toast(`Server ${act.replace("_server", "")} signal sent`, "ok");
    setTimeout(loadInstances, 3500);
  } catch (e) {
    toast(e.message, "err");
    btn.disabled = false;
  }
}
window.executeCardPower = executeCardPower;

async function openInstance(id) {
  if (!(await confirmLeaveConfig())) return;
  state.cfgPath = null;
  state.cfgLoaded = "";
  state.cfgEditable = false;
  state.cfgMeta = "";
  showView("instance");
  $("#instName").textContent = "Loading instance details…";
  $("#instMeta").innerHTML = "";
  try {
    const d = await api(`/api/instances/${id}`);
    state.inst = {
      id,
      server: d.server,
      manifest: d.manifest,
      stats: d.stats,
      minecraft: d.manifest.minecraft,
      loader: d.manifest.loader,
      pack: d.manifest.pack,
    };
    renderInstanceHead();
    showTab("mods");
  } catch (e) {
    toast(e.message, "err");
  }
}
window.openInstance = openInstance;

function renderInstanceHead() {
  const i = state.inst;
  const running = Boolean(i.stats && i.stats.running);
  $("#crumbName").textContent = i.server.server_name;
  $("#instName").textContent = i.server.server_name;
  
  const statePill = $("#instState");
  statePill.className = "pill pill-status " + (running ? "pill-running" : "");
  statePill.innerHTML = `<span class="pill-dot"></span><span class="pill-text">${running ? "Running" : "Stopped"}</span>`;

  const bits = [];
  if (i.pack) {
    // An imported pack usually carries no version, so this has to read
    // correctly with the version half empty.
    const label = [i.pack.name || "Imported pack", i.pack.version || ""].filter(Boolean).join(" · ");
    bits.push(`<span class="pill ok">${esc(label)}</span>`);
  } else {
    bits.push('<span class="pill ghost">unmanaged</span>');
  }
  if (i.loader) bits.push(`<span class="pill mono">${esc(i.loader)}</span>`);
  if (i.minecraft) bits.push(`<span class="pill mono">${esc(i.minecraft)}</span>`);
  bits.push(`<span class="pill mono" title="Server port">:${esc(i.server.server_port)}</span>`);
  if (running && i.stats && i.stats.online != null) {
    bits.push(`<span class="pill">${esc(String(i.stats.online))} online</span>`);
  }
  $("#instMeta").innerHTML = bits.join(" ");
}

$$("[data-power]").forEach((b) => {
  b.onclick = async () => {
    b.disabled = true;
    try {
      const r = await api(
        `/api/instances/${state.inst.id}/action/${b.dataset.power}`,
        { method: "POST" });
      const prepared = r.prepared || {};
      if (prepared.java) toast(`Java corrected to ${prepared.java}`, "ok");
      if (prepared.eula) toast("eula.txt normalised for Crafty", "ok");
      toast(b.dataset.power.replace("_", " ") + " signal sent", "ok");
      setTimeout(async () => {
        const d = await api(`/api/instances/${state.inst.id}`);
        state.inst.stats = d.stats;
        renderInstanceHead();
      }, 4000);
    } catch (e) {
      toast(e.message, "err");
    } finally {
      b.disabled = false;
    }
  };
});

// --- Mods Tab ----------------------------------------------------------

// Selection lives in a Set of filenames rather than in the DOM: a
// virtualised row that scrolls out of the window has no checkbox to read, and
// the user's selection has to survive it being unmounted and remounted.
const modSel = new Set();

// Above this many rows the list renders a window instead of every row. Below
// it, a 40-mod pack should not pay for the machinery.
const VIRT_THRESHOLD = 120;
const VIRT_ROW_H = 52;      // comfortable row height, fixed while virtualised
const VIRT_OVERSCAN = 20;   // rows rendered either side of the viewport

const DELETE_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M5 7h14M9 7V5h6v2m-8 0l1 12h8l1-12"></path></svg>`;

function setTabCount(tab, value) {
  const el = $(`.tab-count[data-tab-count="${tab}"]`);
  if (el) el.textContent = value == null ? "" : String(value);
}

async function loadMods({ keepScroll = false } = {}) {
  const host = $("#modList");
  const scrollTop = host.scrollTop;
  if (!keepScroll || !host.children.length) {
    host.innerHTML = `
      <div class="skel-row"><span class="skel skel-tile"></span><span class="skel skel-line w40"></span></div>
      <div class="skel-row"><span class="skel skel-tile"></span><span class="skel skel-line w60"></span></div>
      <div class="skel-row"><span class="skel skel-tile"></span><span class="skel skel-line w25"></span></div>
      <div class="skel-row"><span class="skel skel-tile"></span><span class="skel skel-line w40"></span></div>`;
  }
  try {
    const r = await api(`/api/instances/${state.inst.id}/mods`);
    state.mods = r.mods || [];
    // Drop selections for jars that no longer exist.
    const live = new Set(state.mods.map((m) => m.file));
    [...modSel].forEach((f) => { if (!live.has(f)) modSel.delete(f); });
    setTabCount("mods", state.mods.length);
    updateModStats();
    renderMods();
    if (keepScroll) host.scrollTop = scrollTop;

    // Fetch missing icons in background and repaint
    const missing = state.mods.some((m) => m.identified && !m.logo);
    if (missing) {
      api(`/api/instances/${state.inst.id}/mods/icons`, { method: "POST" })
        .then((res) => { if (res.updated) loadMods({ keepScroll: true }); })
        .catch(() => {});
    }
  } catch (e) {
    host.innerHTML = `<div class="empty"><div class="t">Could not read the mods folder</div><div class="d">${esc(e.message)}</div></div>`;
  }
}

// Does this mod pass the filter controls as they stand right now?
function modMatchesFilter(m) {
  const filter = $("#modFilter").value.toLowerCase();
  const mode = $("#modState").value;
  if (mode === "enabled" && !m.enabled) return false;
  if (mode === "disabled" && m.enabled) return false;
  if (mode === "unidentified" && m.identified) return false;
  if (!filter) return true;
  return (m.name || "").toLowerCase().includes(filter) ||
         (m.file || "").toLowerCase().includes(filter);
}

function modRowHTML(m) {
  const f = esc(m.file);
  const label = esc(m.name || m.file);
  const selected = modSel.has(m.file);
  return `
    <div class="modrow ${m.enabled ? "" : "off"} ${selected ? "selected" : ""}" data-file="${f}">
      <label class="custom-checkbox">
        <input type="checkbox" class="modSel" data-file="${f}" ${selected ? "checked" : ""}
               aria-label="Select ${label}">
        <span class="checkbox-mark"></span>
      </label>
      ${iconHTML(m.logo, m.name || m.file)}
      <div class="mod-main">
        <div class="mod-name">
          <span>${label}</span>
          ${m.client_only_guess ? '<span class="pill warn" title="May be a client-only mod">client?</span>' : ""}
          ${m.required_by ? `<span class="pill info" title="Installed as a dependency of ${esc(m.required_by)}">dep</span>` : ""}
          ${m.identified === false ? '<span class="pill ghost">unidentified</span>' : ""}
        </div>
        <div class="mod-sub">${f}${m.size ? " · " + esc(m.size) : ""}${m.required_by ? " · required by " + esc(m.required_by) : ""}</div>
      </div>
      <div class="mod-ver">
        ${m.version
          ? `<span class="cur mono">${esc(m.version)}</span>`
          : `<span class="cur na" title="Run Identify Unknown to match this jar to a project">n/a</span>`}
        <button class="btn btn-sm btn-ghost" data-ver="${f}"
                aria-label="${m.project_id ? "Change version of" : "Identify"} ${label}">${m.project_id ? "Change…" : "Identify"}</button>
      </div>
      <div class="mod-actions">
        <label class="switch" title="${m.enabled ? "Disable" : "Enable"} mod">
          <input type="checkbox" class="modToggle" data-file="${f}" ${m.enabled ? "checked" : ""}
                 aria-label="Enable ${label}">
          <span class="track"></span>
        </label>
        <button class="btn btn-sm btn-ghost btn-icon" data-del="${f}" title="Delete jar file"
                aria-label="Delete ${label}">${DELETE_ICON}</button>
      </div>
    </div>`;
}

function renderMods() {
  state.modView = state.mods.filter(modMatchesFilter);
  paintMods();
  updateBulkBar();
}

// Renders either the whole filtered list or, past the threshold, a window of
// it with spacer divs standing in for the rows above and below. The spacers
// keep the scroll height constant, so toggling a mod (a rename, not a resize)
// never moves the list under the cursor.
function paintMods() {
  const host = $("#modList");
  const rows = state.modView;

  if (!rows.length) {
    host.innerHTML = `<div class="empty"><div class="t">Nothing matches</div><div class="d">No mods match the current filter.</div></div>`;
    return;
  }

  if (rows.length <= VIRT_THRESHOLD) {
    state.modVirt = false;
    host.innerHTML = rows.map(modRowHTML).join("");
    return;
  }

  state.modVirt = true;
  const viewport = host.clientHeight || 600;
  const first = Math.max(0, Math.floor(host.scrollTop / VIRT_ROW_H) - VIRT_OVERSCAN);
  const visible = Math.ceil(viewport / VIRT_ROW_H) + VIRT_OVERSCAN * 2;
  const last = Math.min(rows.length, first + visible);
  const padTop = first * VIRT_ROW_H;
  const padBottom = (rows.length - last) * VIRT_ROW_H;

  host.innerHTML =
    `<div class="virt-space" style="height:${padTop}px"></div>` +
    rows.slice(first, last).map(modRowHTML).join("") +
    `<div class="virt-space" style="height:${padBottom}px"></div>`;
  state.modWindow = [first, last];
}

// Bound once, on the container, so rows that mount and unmount during
// virtualised scrolling never need rebinding.
function bindModList() {
  const host = $("#modList");

  host.addEventListener("scroll", () => {
    if (!state.modVirt) return;
    const first = Math.max(0, Math.floor(host.scrollTop / VIRT_ROW_H) - VIRT_OVERSCAN);
    const [prev] = state.modWindow || [-999];
    if (Math.abs(first - prev) < 5) return;   // repaint only when it matters
    paintMods();
  }, { passive: true });

  host.addEventListener("change", async (e) => {
    const t = e.target;

    if (t.classList.contains("modSel")) {
      if (t.checked) modSel.add(t.dataset.file); else modSel.delete(t.dataset.file);
      const row = t.closest(".modrow");
      if (row) row.classList.toggle("selected", t.checked);
      updateBulkBar();
      return;
    }

    if (t.classList.contains("modToggle")) {
      const previous = t.dataset.file;
      const enabled = t.checked;
      t.disabled = true;
      try {
        const res = await api(`/api/instances/${state.inst.id}/mods/toggle`, {
          method: "POST",
          body: { file: previous, enabled },
        });
        applyModState(previous, res.file || previous, enabled);
      } catch (err) {
        toast(err.message, "err");
        t.checked = !enabled;
      } finally {
        t.disabled = false;
      }
    }
  });

  host.addEventListener("click", async (e) => {
    const del = e.target.closest("[data-del]");
    if (del) {
      const file = del.dataset.del;
      const ok = await confirmDialog({
        title: "Delete mod",
        message: `Permanently delete ${file}? The jar is removed from the server and this cannot be undone.`,
        confirmLabel: "Delete mod",
        danger: true,
      });
      if (!ok) return;
      try {
        await api(`/api/instances/${state.inst.id}/mods/delete`,
          { method: "POST", body: { files: [file] } });
        toast("Mod deleted", "ok");
        removeModRows([file]);
      } catch (err) {
        toast(err.message, "err");
      }
      return;
    }

    const ver = e.target.closest("[data-ver]");
    if (ver) {
      const mod = state.mods.find((m) => m.file === ver.dataset.ver);
      if (mod) openVersionPicker(mod);
    }
  });
}

// Repaint one mod row after a toggle, keeping the rest of the list -- and the
// user's scroll position and selection -- exactly where it was. The data array
// is patched first; the DOM row only if it happens to be mounted, since a
// virtualised row outside the window has no node at all.
function applyModState(previousFile, newFile, enabled) {
  const mod = state.mods.find((m) => m.file === previousFile);
  if (mod) {
    mod.file = newFile;
    mod.enabled = enabled;
    if (mod.path) mod.path = mod.path.replace(/[^/]+$/, newFile);
  }
  if (modSel.delete(previousFile)) modSel.add(newFile);

  const row = $(`#modList .modrow[data-file="${cssEscape(previousFile)}"]`);
  if (row) {
    row.dataset.file = newFile;
    row.classList.toggle("off", !enabled);
    $$("[data-file]", row).forEach((el) => { el.dataset.file = newFile; });
    const del = $("[data-del]", row);
    if (del) del.dataset.del = newFile;
    const ver = $("[data-ver]", row);
    if (ver) ver.dataset.ver = newFile;
    const sub = $(".mod-sub", row);
    if (sub) {
      sub.textContent = newFile
        + (mod && mod.size ? " · " + mod.size : "")
        + (mod && mod.required_by ? " · required by " + mod.required_by : "");
    }
    const track = $(".switch", row);
    if (track) track.title = enabled ? "Disable mod" : "Enable mod";
    // A row that no longer matches the active filter stays put and says so,
    // rather than vanishing from under the cursor.
    if (mod) row.classList.toggle("stale", !modMatchesFilter(mod));
  }
  updateModStats();
  updateBulkBar();
}

function removeModRows(files) {
  const gone = new Set(files);
  state.mods = state.mods.filter((m) => !gone.has(m.file));
  state.modView = (state.modView || []).filter((m) => !gone.has(m.file));
  files.forEach((f) => modSel.delete(f));
  $$("#modList .modrow")
    .filter((r) => gone.has(r.dataset.file))
    .forEach((r) => r.remove());
  if (!$$("#modList .modrow").length) {
    $("#modList").innerHTML = `<div class="empty"><div class="t">Nothing matches</div><div class="d">No mods match the current filter.</div></div>`;
  }
  $("#selAll").checked = false;
  setTabCount("mods", state.mods.length);
  updateModStats();
  updateBulkBar();
}

// CSS.escape is not universal on older WebViews; the filenames here only ever
// need quotes and backslashes neutralised for an attribute selector.
const cssEscape = (v) => String(v).replace(/["\\]/g, "\\$&");

function updateModStats() {
  const total = state.mods.length;
  const on = state.mods.filter((m) => m.enabled).length;
  $("#modStats").textContent = `${total} mods · ${on} enabled · ${total - on} disabled`;
}

const selectedFiles = () => [...modSel];

function updateBulkBar() {
  const n = modSel.size;
  $("#bulkBar").classList.toggle("hidden", n === 0);
  $("#selCount").textContent = `${n} selected`;
}

$("#selAll").onchange = (e) => {
  const on = e.target.checked;
  (state.modView || []).forEach((m) => { if (on) modSel.add(m.file); else modSel.delete(m.file); });
  $$("#modList .modSel").forEach((c) => {
    c.checked = on;
    const row = c.closest(".modrow");
    if (row) row.classList.toggle("selected", on);
  });
  updateBulkBar();
};

$$("[data-bulk]").forEach((b) => {
  b.onclick = async () => {
    const files = selectedFiles();
    if (!files.length) return;
    const act = b.dataset.bulk;
    if (act === "delete") {
      const ok = await confirmDialog({
        title: "Delete selected mods",
        message: `Permanently delete ${files.length} selected mod${files.length === 1 ? "" : "s"}? This cannot be undone.`,
        detail: files.join("\n"),
        confirmLabel: `Delete ${files.length} mod${files.length === 1 ? "" : "s"}`,
        danger: true,
      });
      if (!ok) return;
    }
    b.disabled = true;
    try {
      if (act === "delete") {
        await api(`/api/instances/${state.inst.id}/mods/delete`,
          { method: "POST", body: { files } });
        removeModRows(files);
        toast(`Deleted ${files.length} mods`, "ok");
      } else {
        const enabled = act === "enable";
        const res = await api(`/api/instances/${state.inst.id}/mods/bulk-toggle`,
          { method: "POST", body: { files, enabled } });
        (res.changed || []).forEach((c) => {
          if (c.changed) applyModState(c.previous, c.file, c.enabled);
        });
        (res.errors || []).forEach((err) => toast(`${err.file}: ${err.error}`, "err"));
        $$(".modToggle", $("#modList")).forEach((t) => {
          const mod = state.mods.find((m) => m.file === t.dataset.file);
          if (mod) t.checked = mod.enabled;
        });
        $("#selAll").checked = false;
        modSel.clear();
        $$("#modList .modSel").forEach((c) => {
          c.checked = false;
          const row = c.closest(".modrow");
          if (row) row.classList.remove("selected");
        });
        updateBulkBar();
        toast(`${enabled ? "Enabled" : "Disabled"} ${files.length} mods`, "ok");
      }
    } catch (e) {
      toast(e.message, "err");
    } finally {
      b.disabled = false;
    }
  };
});

$("#modFilter").oninput = renderMods;
$("#modState").onchange = renderMods;

// --- Mod Version Switcher ----------------------------------------------

async function openVersionPicker(mod) {
  const i = state.inst;
  const m = modal({
    title: `Versions — ${mod.name || mod.file}`,
    wide: true,
    body: `
      <div class="row" style="margin-bottom:14px">
        ${iconHTML(mod.logo, mod.name)}
        <div>
          <div style="font-size:15px"><strong>${esc(mod.name || mod.file)}</strong></div>
          <div class="faint">Installed <span class="pill mono">${esc(mod.version || "unknown")}</span> from ${esc(mod.source || "CurseForge")}</div>
        </div>
      </div>
      <label class="custom-checkbox" style="margin-bottom:12px">
        <input type="checkbox" id="vOnlyCompat" checked>
        <span class="checkbox-mark"></span>
        <span class="checkbox-label">Filter to ${esc(i.loader || "current loader")} for Minecraft ${esc(i.minecraft || "")}</span>
      </label>
      <div id="vBody"><div class="loading-line"><span class="spin"></span> Fetching releases…</div></div>`,
  });

  const load = async () => {
    const body = $("#vBody", m.el);
    body.innerHTML = `<div style="text-align:center;padding:24px"><span class="spin"></span> Loading releases…</div>`;
    const params = new URLSearchParams();
    if ($("#vOnlyCompat", m.el).checked) {
      if (i.minecraft) params.set("game_version", i.minecraft);
      if (i.loader) params.set("loader", i.loader);
    }
    try {
      const r = await api(`/api/mods/${mod.source || "curseforge"}/${mod.project_id}/versions?` + params);
      if (!r.items.length) {
        body.innerHTML = `<div class="empty"><div class="t">No compatible releases</div>
          <div class="d">Nothing published for this loader and Minecraft version. Untick the filter
            above to see every release.</div></div>`;
        return;
      }
      body.innerHTML = `
        <div class="table-wrap">
          <table>
            <thead><tr><th>Release</th><th>MC</th><th>Loader</th><th>Channel</th><th>Date</th><th></th></tr></thead>
            <tbody>
              ${r.items.map((f) => {
                const current = String(f.file_id) === String(mod.file_id);
                return `
                  <tr class="${current ? "active" : ""}">
                    <td>${esc(f.display_name || f.version_number)}</td>
                    <td class="mono">${esc((f.game_versions || []).slice(0, 3).join(", "))}</td>
                    <td class="mono">${esc((f.loaders || []).join(", "))}</td>
                    <td><span class="pill ${f.release_type === "release" ? "ok" : f.release_type === "beta" ? "warn" : "err"}">${esc(f.release_type || "release")}</span></td>
                    <td class="faint">${esc((f.date || "").slice(0, 10))}</td>
                    <td style="text-align:right">${current
                      ? '<span class="pill">Installed</span>'
                      : `<button class="btn btn-sm ${f.release_type === "release" ? "btn-primary" : "btn-secondary"}" data-sw="${esc(String(f.file_id))}">Switch</button>`}</td>
                  </tr>`;
              }).join("")}
            </tbody>
          </table>
        </div>`;

      $$("[data-sw]", m.el).forEach((b) => {
        b.onclick = async () => {
          b.disabled = true;
          try {
            const res = await api(`/api/instances/${i.id}/mods/add`, {
              method: "POST",
              body: {
                source: mod.source || "curseforge",
                project_id: mod.project_id,
                file_id: b.dataset.sw,
                replace_file: mod.file,
                with_dependencies: true,
                name: mod.name,
              },
            });
            m.close();
            followJob(res.job_id, `Switching ${mod.name || mod.file}`,
                      () => loadMods({ keepScroll: true }));
          } catch (e) {
            m.error(e.message);
            b.disabled = false;
          }
        };
      });
    } catch (e) {
      body.innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
    }
  };

  $("#vOnlyCompat", m.el).onchange = load;
  load();
}

// --- Add Mod Modal (with dependency resolution) ------------------------

$("#addModBtn").onclick = () => openAddMod();

// Reduce a mod name or jar filename to something comparable, so a search
// result can be matched against what is already installed even when the mod
// was never identified (pack-installed jars carry no project id).
function modKey(text) {
  return String(text || "")
    .replace(/\.jar(\.disabled)?$/i, "")
    .replace(/[-_+]v?\d[\w.+]*$/i, "")
    .replace(/[^a-z0-9]/gi, "")
    .toLowerCase();
}

// Is this catalogue entry already in the open instance? Exact project-id
// match first; fall back to the name, which is what catches mods that came
// in with a modpack and have never been identified.
function findInstalled(p) {
  const mods = state.mods || [];
  const byId = mods.find(
    (m) => m.project_id != null && String(m.project_id) === String(p.id) &&
           (!m.source || !p.source || m.source === p.source));
  if (byId) return byId;
  const key = modKey(p.name);
  if (!key) return null;
  return mods.find((m) => modKey(m.name) === key || modKey(m.file) === key);
}

// "Latest" the way a person means it: the newest stable build, falling back
// to newest of anything when a mod has no stable release for this version.
function pickLatestVersion(items) {
  if (!items || !items.length) return null;
  const byDate = (a, b) => String(b.date || "").localeCompare(String(a.date || ""));
  const stable = items.filter((f) => String(f.release_type).toLowerCase() === "release");
  return (stable.length ? stable : items).slice().sort(byDate)[0];
}

function openAddMod(prefill = "") {
  const i = state.inst;
  const m = modal({
    title: "Add a mod",
    wide: true,
    body: `
      <div class="row" style="margin-bottom:12px">
        <div class="search-input-wrap">
          <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
          <input type="search" id="amQ" placeholder="Search mods by name or keywords…" value="${esc(prefill)}">
        </div>
        <select id="amSrc" class="custom-select">
          <option value="curseforge">CurseForge</option>
          <option value="modrinth">Modrinth</option>
        </select>
        <button class="btn btn-primary" id="amGo">Search</button>
      </div>
      <div class="faint" style="margin-bottom:14px">
        Filtered for <strong>${esc(i.loader || "any loader")}</strong> · Minecraft <strong>${esc(i.minecraft || "any")}</strong>. Dependencies resolved automatically.
      </div>
      <div id="amResults"></div>`,
  });

  const run = async () => {
    const host = $("#amResults", m.el);
    host.innerHTML = `<div class="loading-line"><span class="spin"></span> Searching the catalogue…</div>`;
    const params = new URLSearchParams({
      q: $("#amQ", m.el).value.trim(),
      source: $("#amSrc", m.el).value,
      page_size: 20,
    });
    if (i.minecraft) params.set("game_version", i.minecraft);
    if (i.loader) params.set("loader", i.loader);
    try {
      const r = await api("/api/browse/mods?" + params);
      host.innerHTML = r.items.length ? `
        <div class="pack-grid">
          ${r.items.map((p) => {
            const have = findInstalled(p);
            return `
            <article class="pack ${have ? "already-installed" : ""}" style="cursor:default">
              <div class="pack-head">
                ${iconHTML(p.logo, p.name, "mod-icon")}
                <div class="body">
                  <div class="title">${esc(p.name)}</div>
                  <div class="downloads">${esc(p.source === "modrinth" ? "Modrinth" : "CurseForge")} · ${num(p.downloads)}</div>
                </div>
              </div>
              <p class="summary">${esc(p.summary || "")}</p>
              <div class="meta">
                ${have ? `<span class="pill ok">Installed ${esc(have.version || have.file)}</span>` : ""}
                ${p.server_side === "unsupported" ? '<span class="pill err">client-side only</span>' : ""}
                <div class="spacer"></div>
                <button class="btn btn-sm btn-primary"
                        data-add="${esc(String(p.id))}" data-src="${esc(p.source)}"
                        data-name="${esc(p.name)}"
                        data-have="${have ? esc(have.file) : ""}"
                        title="${have ? "Replace the installed copy with the latest build" : "Install the latest compatible build"}">
                  ${have ? "Update" : "Add latest"}
                </button>
                <button class="btn btn-sm btn-ghost" data-pick="${esc(String(p.id))}"
                        data-src="${esc(p.source)}" data-name="${esc(p.name)}"
                        data-have="${have ? esc(have.file) : ""}"
                        title="Pick a specific version instead">Versions</button>
              </div>
            </article>`;
          }).join("")}
        </div>` : `
        <div class="empty"><div class="t">No matching mods</div>
          <div class="d">Nothing matched for ${esc(i.loader || "this loader")} on Minecraft
            ${esc(i.minecraft || "this version")}. Try the other source, or a shorter search.</div></div>`;

      $$("[data-pick]", m.el).forEach((b) => {
        b.onclick = () => pickModVersion(b.dataset.src, b.dataset.pick, b.dataset.name,
                                         m, b.dataset.have || null);
      });
      // Default path: skip the version list entirely and take the newest
      // stable build. Choosing a version is still one click away.
      $$("[data-add]", m.el).forEach((b) => {
        b.onclick = () => addLatestVersion(b.dataset.src, b.dataset.add,
                                           b.dataset.name, m, b.dataset.have || null);
      });
    } catch (e) {
      host.innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
    }
  };

  $("#amGo", m.el).onclick = run;
  $("#amQ", m.el).addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
  if (prefill) run();
  return m;
}

// One-click install: resolve the newest compatible build and go straight to
// the dependency preview. `replaceFile` is set when the mod is already in the
// instance, so an update overwrites the old jar instead of leaving two copies
// for the loader to trip over.
async function addLatestVersion(source, projectId, name, parent, replaceFile) {
  const i = state.inst;
  const host = $("#amResults", parent.el);
  host.innerHTML = `<div style="text-align:center;padding:24px"><span class="spin"></span>
      Finding the latest build of ${esc(name)}…</div>`;
  const params = new URLSearchParams();
  if (i.minecraft) params.set("game_version", i.minecraft);
  if (i.loader) params.set("loader", i.loader);
  try {
    const r = await api(`/api/mods/${source}/${projectId}/versions?` + params);
    const latest = pickLatestVersion(r.items);
    if (!latest) {
      host.innerHTML = `<div class="empty">No compatible releases for
        ${esc(i.loader || "")} ${esc(i.minecraft || "")}.
        <div style="margin-top:10px"><button class="btn btn-sm" id="amBack">Back to results</button></div></div>`;
      const back = $("#amBack", parent.el);
      if (back) back.onclick = () => $("#amGo", parent.el).click();
      return;
    }
    previewDependencies(source, projectId, latest.file_id, name, parent, replaceFile);
  } catch (e) {
    host.innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
  }
}

async function pickModVersion(source, projectId, name, parent, replaceFile) {
  const i = state.inst;
  const host = $("#amResults", parent.el);
  host.innerHTML = `<div style="text-align:center;padding:24px"><span class="spin"></span> Loading releases for ${esc(name)}…</div>`;
  const params = new URLSearchParams();
  if (i.minecraft) params.set("game_version", i.minecraft);
  if (i.loader) params.set("loader", i.loader);
  try {
    const r = await api(`/api/mods/${source}/${projectId}/versions?` + params);
    if (!r.items.length) {
      host.innerHTML = `<div class="empty">No compatible releases for ${esc(i.loader || "")} ${esc(i.minecraft || "")}.</div>`;
      return;
    }
    host.innerHTML = `
      <h3 style="margin-bottom:12px">${esc(name)} — Select a Release</h3>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Release Name</th>
              <th>MC Version</th>
              <th>Loader</th>
              <th>Channel</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            ${r.items.map((f) => `
              <tr>
                <td><strong>${esc(f.display_name || f.version_number)}</strong></td>
                <td class="faint mono">${esc((f.game_versions || []).slice(0, 3).join(", "))}</td>
                <td class="faint mono">${esc((f.loaders || []).join(", "))}</td>
                <td><span class="pill ${f.release_type === "release" ? "ok" : "warn"}">${esc(f.release_type || "release")}</span></td>
                <td style="text-align:right">
                  <button class="btn btn-sm btn-primary" data-add="${esc(String(f.file_id))}">Choose</button>
                </td>
              </tr>`).join("")}
          </tbody>
        </table>
      </div>`;

    $$("[data-add]", parent.el).forEach((b) => {
      b.onclick = () => previewDependencies(source, projectId, b.dataset.add, name,
                                            parent, replaceFile);
    });
  } catch (e) {
    host.innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
  }
}

async function previewDependencies(source, projectId, fileId, name, parent, replaceFile) {
  const host = $("#amResults", parent.el);
  host.innerHTML = `<div class="loading-line"><span class="spin"></span> Resolving the dependency graph…</div>`;
  try {
    const plan = await api(`/api/instances/${state.inst.id}/mods/resolve`, {
      method: "POST",
      body: { source, project_id: projectId, file_id: fileId },
    });
    const dep = plan.dependencies || [];
    const rows = dep.map((d) => `
      <div class="reviewrow">
        <label class="custom-checkbox">
          <input type="checkbox" class="depSel" data-dep="${esc(String(d.project_id))}"
                 data-pid="${esc(String(d.project_id))}" checked
                 aria-label="Install ${esc(d.name)}">
          <span class="checkbox-mark"></span>
        </label>
        ${iconHTML(d.logo, d.name)}
        <div class="body">
          <div class="t">${esc(d.name)} <span class="pill mono">${esc(d.version || "")}</span></div>
          <div class="reasons">Required by <strong>${esc(d.required_by || name)}</strong>${d.size ? " · " + mb(d.size) : ""}</div>
        </div>
      </div>`).join("");

    const satisfied = (plan.already_satisfied || []).length;
    host.innerHTML = `
      <div class="summary-note info">
        <span class="glyph">i</span>
        <span>${satisfied ? `${satisfied} dependenc${satisfied === 1 ? "y" : "ies"} already satisfied · ` : ""}
          ${dep.length ? `${dep.length} will be downloaded` : "nothing else to download"}
          ${plan.total_bytes ? ` (${mb(plan.total_bytes)})` : ""}</span>
      </div>
      ${replaceFile ? `
        <div class="summary-note warn" style="margin-top:10px">
          <span class="glyph">!</span>
          <span>Already installed as <code>${esc(replaceFile)}</code> — it is replaced, not added
            alongside, so the loader never sees two copies.</span>
        </div>` : ""}
      ${(plan.warnings || []).map((w) => `
        <div class="summary-note warn" style="margin-top:10px"><span class="glyph">!</span><span>${esc(w)}</span></div>`).join("")}

      <div class="card flush" style="margin-top:10px">
        ${plan.root ? `
          <div class="reviewrow keep">
            ${iconHTML(plan.root.logo, plan.root.name)}
            <div class="body">
              <div class="t">${esc(plan.root.name)} <span class="pill mono">${esc(plan.root.version || "")}</span></div>
              <div class="reasons">The mod you picked</div>
            </div>
          </div>` : ""}
        ${rows}
        <div class="apply-bar">
          <span class="faint">Uncheck anything you already manage yourself.</span>
          <div class="spacer"></div>
          <button class="btn btn-primary" id="depGo">
            ${replaceFile ? "Update" : "Install"} ${dep.length ? dep.length + 1 : 1} mod${dep.length ? "s" : ""}
          </button>
        </div>
      </div>`;

    $("#depGo", parent.el).onclick = async () => {
      const skip = $$(".depSel:not(:checked)", parent.el).map((c) => c.dataset.pid);
      try {
        const res = await api(`/api/instances/${state.inst.id}/mods/add`, {
          method: "POST",
          body: {
            source,
            project_id: projectId,
            file_id: fileId,
            with_dependencies: true,
            skip_dependencies: skip,
            replace_file: replaceFile || undefined,
            name,
          },
        });
        parent.close();
        followJob(res.job_id, `${replaceFile ? "Updating" : "Installing"} ${name}`,
                  () => loadMods({ keepScroll: true }));
      } catch (e) {
        parent.error(e.message);
      }
    };
  } catch (e) {
    host.innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
  }
}

$("#identifyMods").onclick = async () => {
  try {
    const r = await api(`/api/instances/${state.inst.id}/mods/identify`, { method: "POST" });
    followJob(r.job_id, "Identifying Mods via Fingerprints",
              () => loadMods({ keepScroll: true }));
  } catch (e) {
    toast(e.message, "err");
  }
};

$("#checkUpdates").onclick = async () => {
  const btn = $("#checkUpdates");
  btn.disabled = true;
  try {
    const r = await api(`/api/instances/${state.inst.id}/mods/updates`);
    if (r.note) { toast(r.note); return; }
    const m = modal({
      title: `Mod updates — ${r.updates.length} of ${r.checked} have newer builds`,
      wide: true,
      body: r.updates.length ? `
        <div class="table-wrap">
          <table>
            <thead><tr><th>Mod</th><th>Installed</th><th>Latest</th><th></th></tr></thead>
            <tbody>
              ${r.updates.map((u, idx) => `
                <tr>
                  <td>${esc(u.name || u.file)}</td>
                  <td class="mono faint">${esc(u.current_version || "—")}</td>
                  <td class="mono">${esc(u.latest_version || "—")}</td>
                  <td style="text-align:right">
                    <button class="btn btn-sm btn-primary" data-up="${idx}">Update</button>
                  </td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>`
        : `<div class="empty"><div class="t">Everything is current</div>
             <div class="d">All ${r.checked} identified mods are on their latest compatible build.</div></div>`,
    });

    $$("[data-up]", m.el).forEach((b) => {
      b.onclick = async () => {
        const u = r.updates[Number(b.dataset.up)];
        b.disabled = true;
        try {
          const res = await api(`/api/instances/${state.inst.id}/mods/add`, {
            method: "POST",
            body: {
              source: u.source,
              project_id: u.project_id,
              file_id: u.latest_file_id,
              replace_file: u.file,
              with_dependencies: true,
              name: u.name,
            },
          });
          b.textContent = "Queued";
          followJob(res.job_id, `Updating ${u.name}`,
                    () => loadMods({ keepScroll: true }));
        } catch (e) {
          m.error(e.message);
          b.disabled = false;
        }
      };
    });
  } catch (e) {
    toast(e.message, "err");
  } finally {
    btn.disabled = false;
  }
};

// --- Configs Tab -------------------------------------------------------

async function loadConfigs() {
  $("#cfgList").innerHTML = `<div class="loading-line"><span class="spin"></span> Scanning configuration files…</div>`;
  try {
    const r = await api(`/api/instances/${state.inst.id}/configs`);
    state.configs = r.files || [];
    setTabCount("configs", state.configs.length);
    renderConfigList();
  } catch (e) {
    $("#cfgList").innerHTML = `<div class="loading-line">${esc(e.message)}</div>`;
  }
}

function renderConfigList() {
  const f = $("#cfgFilter").value.toLowerCase();
  const files = state.configs.filter((c) => !f || c.path.toLowerCase().includes(f));
  const groups = {};
  files.forEach((c) => (groups[c.owner] = groups[c.owner] || []).push(c));
  const dirty = isCfgDirty();

  $("#cfgList").innerHTML = Object.keys(groups).sort().map((owner) =>
    `<div class="group">${esc(owner)} (${groups[owner].length})</div>` +
    groups[owner].map((c) => {
      const active = c.path === state.cfgPath;
      return `<button class="item ${active ? "active" : ""}" type="button"
              data-path="${esc(c.path)}" title="${esc(c.path)}">
        <span class="name">${esc(c.name)}</span>
        ${active && dirty ? '<span class="cfg-dirty-dot" title="Unsaved changes"></span>' : ""}
        ${c.size ? `<span class="size">${esc(c.size)}</span>` : ""}
      </button>`;
    }).join("")).join("")
    || `<div class="empty"><div class="t">No config files</div><div class="d">Nothing under this server's config directories matches.</div></div>`;

  $$("#cfgList .item[data-path]").forEach((el) => {
    el.onclick = () => openConfig(el.dataset.path);
  });
}

// The file list carries the dirty marker too, so an unsaved file is visible
// while the user is looking at a different part of the tree.
function renderCfgDirtyDot() {
  const active = $("#cfgList .item.active");
  if (!active) return;
  const has = $(".cfg-dirty-dot", active);
  if (isCfgDirty() && !has) {
    const dot = document.createElement("span");
    dot.className = "cfg-dirty-dot";
    dot.title = "Unsaved changes";
    active.insertBefore(dot, $(".size", active) || null);
  } else if (!isCfgDirty() && has) {
    has.remove();
  }
}

// A <pre> of line numbers sharing the textarea's font metrics. No highlighting
// overlay: it drifts on wrap and is a maintenance trap. Written on load and on
// input, with scrollTop mirrored from the textarea.
function renderCfgGutter() {
  const gutter = $("#cfgGutter");
  if (!gutter) return;
  const lines = $("#cfgEditor").value.split("\n").length;
  const want = Math.max(lines, 1);
  if (Number(gutter.dataset.lines) === want) return;
  gutter.dataset.lines = String(want);
  let out = "";
  for (let i = 1; i <= want; i++) out += i + "\n";
  gutter.textContent = out;
}

// The editor writes straight to the instance, so switching files or leaving
// the tab with pending edits used to discard them with no warning at all.
function isCfgDirty() {
  return Boolean(state.cfgPath) && state.cfgEditable
    && $("#cfgEditor").value !== state.cfgLoaded;
}

function renderCfgStatus() {
  const status = $("#cfgStatus");
  const dirty = isCfgDirty();
  status.classList.toggle("dirty", dirty);
  status.textContent = state.cfgPath
    ? (dirty ? `Unsaved changes · ${state.cfgMeta}` : state.cfgMeta)
    : "";
  $("#cfgSave").disabled = !state.cfgEditable || !dirty;
  renderCfgDirtyDot();
  renderCfgGutter();
}

async function confirmLeaveConfig() {
  if (!isCfgDirty()) return true;
  const ok = await confirmDialog({
    title: "Discard unsaved changes?",
    message: `${state.cfgPath} has edits that have not been saved to the server. Leaving now discards them.`,
    confirmLabel: "Discard changes",
    danger: true,
  });
  if (ok) {
    state.cfgLoaded = $("#cfgEditor").value;
    renderCfgStatus();
  }
  return ok;
}

async function openConfig(path) {
  if (path !== state.cfgPath && !(await confirmLeaveConfig())) return;
  state.cfgPath = path;
  state.cfgEditable = false;
  state.cfgLoaded = "";
  state.cfgMeta = "";
  renderConfigList();
  $("#cfgPath").textContent = path;
  $("#cfgEditor").value = "";
  $("#cfgGutter").textContent = "";
  $("#cfgGutter").dataset.lines = "";
  $("#cfgStatus").textContent = "Loading…";
  $("#cfgSave").disabled = true;
  try {
    const r = await api(`/api/instances/${state.inst.id}/configs/read?path=${encodeURIComponent(path)}`);
    $("#cfgEditor").value = r.content;
    state.cfgLoaded = r.content;
    state.cfgEditable = Boolean(r.editable);
    $("#cfgEditor").readOnly = !r.editable;
    state.cfgMeta = `${r.lines} lines · ${r.language.toUpperCase()}`
      + (r.editable ? "" : " · read-only");
    renderCfgStatus();
  } catch (e) {
    $("#cfgEditor").value = "";
    $("#cfgEditor").readOnly = true;
    state.cfgMeta = e.message;
    renderCfgStatus();
  }
}

$("#cfgSave").onclick = async () => {
  const btn = $("#cfgSave");
  const sent = $("#cfgEditor").value;
  btn.disabled = true;
  try {
    await api(`/api/instances/${state.inst.id}/configs/write`, {
      method: "POST",
      body: { path: state.cfgPath, content: sent },
    });
    toast(`Saved ${state.cfgPath}`, "ok");
    state.cfgLoaded = sent;
    renderCfgStatus();
  } catch (e) {
    toast(e.message, "err");
    btn.disabled = false;
  }
};
$("#cfgEditor").oninput = renderCfgStatus;
$("#cfgEditor").addEventListener("scroll", () => {
  const gutter = $("#cfgGutter");
  if (gutter) gutter.scrollTop = $("#cfgEditor").scrollTop;
}, { passive: true });
$("#cfgFilter").oninput = renderConfigList;

// Last line of defence: a reload or tab close with pending edits.
window.addEventListener("beforeunload", (e) => {
  if (!isCfgDirty()) return;
  e.preventDefault();
  e.returnValue = "";
});

// --- Diagnostics & Troubleshooting Tab ---------------------------------

$("#runDiag").onclick = runDiagnostics;

$("#runDeep").onclick = async () => {
  try {
    const r = await api(`/api/instances/${state.inst.id}/deep-scan`, { method: "POST" });
    followJob(r.job_id, "Deep Dependency Graph Scan", (d) => {
      if (d.result) renderFindings(d.result.findings, `Deep scan — ${d.result.scanned} jars analysed`);
    }, { auto: true });
  } catch (e) {
    toast(e.message, "err");
  }
};

$("#runAI").onclick = async () => {
  if (!state.ai.available) {
    toast(state.ai.reason || "Local AI Assistant is currently offline", "err");
    return;
  }
  try {
    const r = await api(`/api/instances/${state.inst.id}/ai/analyse`, { method: "POST", body: {} });
    followJob(r.job_id, "AI Diagnostic Analysis", (d) => {
      if (d.result) renderAI(d.result);
    }, { auto: true });
  } catch (e) {
    toast(e.message, "err");
  }
};

async function runDiagnostics() {
  const btn = $("#runDiag");
  btn.disabled = true;
  $("#diagOut").innerHTML = `<div class="loading-line"><span class="spin"></span> Running health checks and reading the crash log…</div>`;
  try {
    const r = await api(`/api/instances/${state.inst.id}/diagnose`);
    renderFindings(r.findings, "Diagnostic Checks");
    if (r.log_tail || r.crash_tail) {
      $("#logCard").classList.remove("hidden");
      $("#logPath").textContent = r.crash_path || r.log_path || "";
      $("#logTail").textContent = (r.crash_tail ? r.crash_tail + "\n\n--- latest.log ---\n\n" : "") + (r.log_tail || "");
    } else {
      $("#logCard").classList.add("hidden");
    }
  } catch (e) {
    $("#diagOut").innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

let lastFindings = [];

// Severity is never carried by hue alone: each finding gets a glyph and a
// spelled-out label as well, and the list is sorted worst-first.
const SEV_ORDER = { critical: 0, error: 1, warning: 2, info: 3 };
const SEV_GLYPH = { critical: "!!", error: "!", warning: "!", info: "i" };

function renderFindings(findings, title) {
  lastFindings = (findings || []).slice().sort(
    (a, b) => (SEV_ORDER[a.severity] ?? 9) - (SEV_ORDER[b.severity] ?? 9));

  if (!lastFindings.length) {
    $("#diagOut").innerHTML = `
      <div class="empty">
        <div class="t">No problems found</div>
        <div class="d">${esc(title)} passed. If the server still will not start, run a deep
          dependency scan or ask the assistant.</div>
      </div>`;
    return;
  }

  $("#diagOut").innerHTML = `
    <span class="section-label">${esc(title)} — ${lastFindings.length} finding${lastFindings.length === 1 ? "" : "s"}</span>
    ${lastFindings.map((f, i) => {
      const sev = String(f.severity || "info");
      return `
        <div class="finding ${esc(sev)}">
          <span class="sev-glyph" aria-hidden="true">${SEV_GLYPH[sev] || "i"}</span>
          <div class="body">
            <div class="t"><span class="sev-label">${esc(sev)}</span>${esc(f.title)}</div>
            <div class="d">${esc(f.detail)}</div>
            ${f.evidence ? `<pre>${esc(f.evidence)}</pre>` : ""}
          </div>
          ${f.fix ? `<button class="btn btn-sm btn-primary fix-btn" data-fix="${i}">Fix Automatically</button>` : ""}
        </div>`;
    }).join("")}`;

  $$("#diagOut [data-fix]").forEach((b) => {
    b.onclick = () => applyFix(lastFindings[Number(b.dataset.fix)].fix, b);
  });
}

async function applyFix(fix, btn) {
  const sid = state.inst.id;
  btn.disabled = true;
  try {
    switch (fix.action) {
      case "accept_eula":
        await api(`/api/instances/${sid}/fix/accept-eula`, { method: "POST" });
        toast("eula.txt written in exact format Crafty requires", "ok");
        break;
      case "raise_ram":
        btn.disabled = false;
        showTab("optimize");
        toast("Adjust the heap allocation in the Optimizer tab");
        return;
      case "set_java":
        await api(`/api/instances/${sid}/fix/java`, { method: "POST", body: { minecraft: fix.minecraft } });
        toast("Java runtime version pinned", "ok");
        break;
      case "disable_mods":
        if (!await confirmDialog({
          title: "Disable mods",
          message: `Disable ${fix.files.length} mod${fix.files.length === 1 ? "" : "s"}? They stay on disk and can be re-enabled at any time.`,
          detail: fix.files.join("\n"),
          confirmLabel: "Disable",
        })) {
          btn.disabled = false;
          return;
        }
        await api(`/api/instances/${sid}/mods/bulk-toggle`, { method: "POST", body: { files: fix.files, enabled: false } });
        toast(`Disabled ${fix.files.length} mod(s)`, "ok");
        break;
      case "install_dependency":
        btn.disabled = false;
        return findDependency(fix.mod_id);
      case "fix_versions":
        btn.disabled = false;
        return fixVersions(fix.files);
      case "retry_mod":
        await api(`/api/instances/${sid}/mods/add`, {
          method: "POST",
          body: { source: "curseforge", project_id: fix.project_id, file_id: fix.file_id, with_dependencies: true },
        });
        toast("Re-installation queued", "ok");
        break;
      case "find_client_only":
        btn.disabled = false;
        showTab("mods");
        $("#modFilter").value = "";
        toast("Look for mods flagged 'client?'");
        return;
      case "edit_file":
        btn.disabled = false;
        showTab("configs");
        setTimeout(() => openConfig(fix.path), 400);
        return;
      default:
        toast("No automated fix handler registered", "warn");
        btn.disabled = false;
        return;
    }
    btn.textContent = "✓ Fixed";
    setTimeout(runDiagnostics, 1000);
  } catch (e) {
    toast(e.message, "err");
    btn.disabled = false;
  }
}

async function fixVersions(files) {
  const m = modal({
    title: "Fix incompatible versions",
    wide: true,
    body: `<div id="fvBody"><div class="loading-line"><span class="spin"></span> Searching compatible releases…</div></div>`,
  });
  try {
    const r = await api(`/api/instances/${state.inst.id}/fix/versions`, { method: "POST", body: { files } });
    const rows = (r.suggestions || []).map((sg, i) => `
      <div class="reviewrow">
        <label class="custom-checkbox">
          <input type="checkbox" class="fvSel" data-i="${i}" checked
                 aria-label="Swap ${esc(sg.name || sg.file)}">
          <span class="checkbox-mark"></span>
        </label>
        ${iconHTML(sg.logo, sg.name || sg.file)}
        <div class="body">
          <div class="t">${esc(sg.name || sg.file)}</div>
          <div class="reasons"><code>${esc(sg.current_version || sg.file)}</code> →
            <code>${esc(sg.suggested_version)}</code></div>
        </div>
      </div>`).join("");

    $("#fvBody", m.el).innerHTML = `
      ${rows ? `
        <div class="faint" style="margin-bottom:10px">Compatible builds found for
          ${esc(r.loader || "")} ${esc(r.minecraft || "")}:</div>
        <div class="card flush">
          ${rows}
          <div class="apply-bar">
            <div class="spacer"></div>
            <button class="btn btn-primary" id="fvGo">Switch selected mods</button>
          </div>
        </div>`
        : `<div class="empty"><div class="t">Nothing to swap</div>
             <div class="d">No compatible replacement build could be resolved automatically.</div></div>`}
      ${(r.unresolved || []).length ? `
        <span class="section-label" style="margin-top:14px">Manual attention required (${r.unresolved.length})</span>
        ${r.unresolved.map((u) => `
          <div class="summary-note warn"><span class="glyph">!</span>
            <span><strong>${esc(u.file)}</strong> — ${esc(u.why)}</span></div>`).join("")}
      ` : ""}`;

    const go = $("#fvGo", m.el);
    if (go) go.onclick = async () => {
      const picks = $$(".fvSel:checked", m.el).map((c) => r.suggestions[Number(c.dataset.i)]);
      go.disabled = true;
      for (const s of picks) {
        try {
          await api(`/api/instances/${state.inst.id}/mods/add`, {
            method: "POST",
            body: {
              source: s.source,
              project_id: s.project_id,
              file_id: s.suggested_file_id,
              replace_file: s.file,
              with_dependencies: false,
            },
          });
          toast(`${s.name} → ${s.suggested_version}`, "ok");
        } catch (e) {
          toast(`${s.name}: ${e.message}`, "err");
        }
      }
      m.close();
      loadMods();
      runDiagnostics();
    };
  } catch (e) {
    $("#fvBody", m.el).innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
  }
}

async function findDependency(modId) {
  const i = state.inst;
  const params = new URLSearchParams();
  if (i.minecraft) params.set("game_version", i.minecraft);
  if (i.loader) params.set("loader", i.loader);
  const m = modal({
    title: "Find missing dependency — " + modId,
    wide: true,
    body: `<div id="depBody"><div class="loading-line"><span class="spin"></span> Searching both catalogues…</div></div>`,
  });
  try {
    const r = await api(`/api/diagnose/dependency/${encodeURIComponent(modId)}?` + params);
    const section = (label, items) => (items && items.length)
      ? `<span class="section-label" style="margin-top:12px">${label}</span>
         <div class="card flush">` + items.map((pr) => `
          <div class="reviewrow">
            ${iconHTML(pr.logo, pr.name)}
            <div class="body">
              <div class="t">${esc(pr.name)}</div>
              <div class="reasons">${num(pr.downloads)} downloads${pr.summary ? " · " + esc(pr.summary.slice(0, 90)) : ""}</div>
            </div>
            <button class="btn btn-sm btn-primary" data-dep="${esc(String(pr.id))}"
                    data-src="${esc(pr.source)}" data-name="${esc(pr.name)}">Use this</button>
          </div>`).join("") + "</div>" : "";

    $("#depBody", m.el).innerHTML = `
      <div class="summary-note info">
        <span class="glyph">i</span>
        <span>The crash names mod id <code>${esc(modId)}</code>. Pick the project it refers to and
          BlessForge hands off to Add Mod.</span>
      </div>` + (section("CurseForge", r.curseforge) + section("Modrinth", r.modrinth) ||
      `<div class="empty"><div class="t">No candidates</div>
         <div class="d">Nothing matched that id — it may be bundled inside another library.</div></div>`);

    $$("[data-dep]", m.el).forEach((b) => {
      b.onclick = () => {
        m.close();
        const parent = openAddMod(modId);
        setTimeout(() => pickModVersion(b.dataset.src, b.dataset.dep, b.dataset.name, parent), 80);
      };
    });
  } catch (e) {
    $("#depBody", m.el).innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
  }
}

// --- AI Diagnostic Panel ------------------------------------------------

// Where the wait actually went. On a CPU-only host most of it is prompt
// evaluation, which happens before a single token appears -- showing that
// is what turns "it hung" into "it read 1,855 tokens at 18/s".
function aiStatsHTML(result) {
  const s = result.stats || {};
  if (!s.total_seconds) return "";
  const bits = [];
  if (s.load_seconds > 1) bits.push(`loaded model ${s.load_seconds}s`);
  if (s.prompt_tokens) {
    bits.push(`read ${s.prompt_tokens.toLocaleString()} tokens in ${s.prompt_seconds}s`
      + (s.prompt_tokens_per_second ? ` (${s.prompt_tokens_per_second}/s)` : ""));
  }
  if (s.output_tokens) {
    bits.push(`wrote ${s.output_tokens.toLocaleString()} in ${s.generate_seconds}s`
      + (s.output_tokens_per_second ? ` (${s.output_tokens_per_second}/s)` : ""));
  }
  return `<div class="ai-stats">${esc(s.total_seconds)}s total — ${esc(bits.join(" · "))}</div>`;
}

const AI_GLYPH = `<span class="ai-glyph"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4l3 3h10l3-3-1 7 2 3-4 2-5 5-5-5-4-2 2-3z" fill="currentColor"></path></svg></span>`;

function renderAI(result) {
  const host = $("#aiOut");
  if (!result.available) {
    host.innerHTML = `
      <div class="banner">
        <div><strong>Assistant unavailable.</strong> ${esc(result.reason || "")}
        ${result.hint ? `<div class="faint" style="margin-top:6px">${esc(result.hint)}</div>` : ""}</div>
      </div>`;
    return;
  }
  if (!result.ok) {
    host.innerHTML = `
      <div class="banner err">
        <div><strong>Analysis failed.</strong> ${esc(result.error || "")}
        ${result.partial ? `<pre class="log" style="margin-top:8px">${esc(result.partial)}</pre>` : ""}</div>
      </div>`;
    return;
  }

  const confClass = { high: "ok", medium: "warn", low: "ghost" }[result.confidence] || "";
  host.innerHTML = `
    <div class="card ai-card">
      <div class="ai-card-head">
        ${AI_GLYPH}
        <h3>Assistant read the crash and the mod list</h3>
        <span class="pill ${confClass}">${esc(result.confidence)} confidence</span>
        <span class="pill mono">${esc(result.model || "")}</span>
      </div>
      <div class="ai-card-body">
        <p class="ai-summary">${esc(result.summary || "")}</p>
        ${aiStatsHTML(result)}
        ${result.notes ? `<div class="ai-notes">${esc(result.notes)}</div>` : ""}

        ${result.actions.length ? `
          <span class="section-label" style="margin-top:16px">Recommended actions</span>
          ${result.actions.map((a, i) => `
            <div class="ai-action">
              <label class="custom-checkbox">
                <input type="checkbox" class="aiSel" data-i="${i}" ${a.major ? "" : "checked"}
                       aria-label="Apply: ${esc(a.description)}">
                <span class="checkbox-mark"></span>
              </label>
              <div class="body">
                <div class="t">${esc(a.description)}
                  ${a.major ? '<span class="pill warn">Major Action</span>'
                            : '<span class="pill ok">Safe</span>'}</div>
                <div class="why">${esc(a.why)}</div>
                <code>${esc(a.action)}(${esc(JSON.stringify(a.args))})</code>
              </div>
            </div>`).join("")}
          <div class="apply-bar">
            <span class="faint">Major actions ask again before running.</span>
            <div class="spacer"></div>
            <button class="btn btn-primary" id="aiApplyAll">Apply Selected Fixes</button>
          </div>
        ` : `<div class="faint" style="margin-top:10px">No automated actions suggested.</div>`}

        ${result.rejected && result.rejected.length ? `
          <div class="faint" style="margin-top:10px">Discarded ${result.rejected.length} invalid model suggestion(s).</div>
        ` : ""}
      </div>
    </div>`;

  const applyBtn = $("#aiApplyAll");
  if (applyBtn) {
    applyBtn.onclick = async () => {
      const picked = $$(".aiSel:checked").map((c) => result.actions[Number(c.dataset.i)]);
      if (!picked.length) { toast("No actions selected"); return; }
      const major = picked.filter((a) => a.major);
      if (major.length) {
        const ok = await confirmDialog({
          title: "Apply AI actions",
          message: `${major.length} of the selected action${major.length === 1 ? " modifies" : "s modify"} or deletes files. Execute them now?`,
          detail: major.map((a) => `• ${a.description}`).join("\n"),
          confirmLabel: "Execute",
          danger: true,
        });
        if (!ok) return;
      }
      applyBtn.disabled = true;
      try {
        const res = await api(`/api/instances/${state.inst.id}/ai/apply`, {
          method: "POST",
          body: { actions: picked, confirmed: true },
        });
        toast(`Applied ${res.applied.length} AI action(s)`, "ok");
        (res.failed || []).forEach((f) => toast(`${f.action}: ${f.error}`, "err"));
        loadMods();
        runDiagnostics();
      } catch (e) {
        toast(e.message, "err");
      } finally {
        applyBtn.disabled = false;
      }
    };
  }
}

// --- Machine Optimizer Tab ---------------------------------------------

async function loadOptimize() {
  const host = $("#optOut");
  host.innerHTML = `<div class="card" style="text-align:center"><span class="spin"></span> Measuring host metrics and tuning profile…</div>`;
  try {
    const p = await api(`/api/instances/${state.inst.id}/optimize`);
    renderOptimize(p);
  } catch (e) {
    host.innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
  }
}

// --- port -------------------------------------------------------------

async function loadPortCard() {
  const host = $("#portCard");
  if (!host) return;
  try {
    const s = await api(`/api/instances/${state.inst.id}/port`);
    const others = (s.in_use_by_others || []).filter((o) => o.port === s.crafty_port);
    const navPort = $("#optNavPort");
    if (navPort) navPort.textContent = String(s.crafty_port ?? "—");
    host.innerHTML = `
      <div class="row">
        <div>
          <span class="section-label">Server port</span>
          <input type="number" id="portInput" class="input-num" min="1024" max="65535"
                 value="${esc(String(s.crafty_port ?? ""))}" style="width:118px" aria-label="Server port">
        </div>
        <label class="custom-checkbox" title="Keep the query protocol on the same port">
          <input type="checkbox" id="portQuery" checked>
          <span class="checkbox-mark"></span>
          <span class="checkbox-label">Also set <code>query.port</code></span>
        </label>
        <div class="spacer"></div>
        <button class="btn btn-secondary" id="portSave">Save Port</button>
      </div>
      ${s.mismatch ? `<div class="banner" style="margin:12px 0 0"><span>${esc(s.note)}</span></div>` : ""}
      <p class="muted" style="margin:11px 0 0">
        Crafty's record and <code>server.properties</code> must agree — setting only one leaves the
        server running but permanently shown offline. Crafty publishes
        ${esc(String(s.published_range[0]))}–${esc(String(s.published_range[1]))}; outside that range the
        server works inside Docker but is not reachable from your network.
        ${others.length ? ` Port ${esc(String(s.crafty_port))} is also claimed by
          <strong>${esc(others.map((o) => o.name).join(", "))}</strong> — only one server can bind it at a time.` : ""}
      </p>`;

    $("#portSave", host).onclick = async () => {
      const btn = $("#portSave", host);
      const port = Number($("#portInput", host).value);
      btn.disabled = true;
      const send = (force) => api(`/api/instances/${state.inst.id}/port`, {
        method: "POST",
        body: { port, update_query: $("#portQuery", host).checked, force },
      });
      try {
        let res;
        try {
          res = await send(false);
        } catch (e) {
          // The backend refuses a port another instance already claims;
          // let the user override deliberately rather than silently.
          if (!/already used by/i.test(e.message)) throw e;
          const ok = await confirmDialog({
            title: "Port already assigned",
            message: e.message,
            confirmLabel: "Assign anyway",
            danger: true,
          });
          if (!ok) { btn.disabled = false; return; }
          res = await send(true);
        }
        toast(`Port set to ${res.port} — restart to apply`, "ok");
        (res.warnings || []).forEach((w) => toast(w, "err"));
        loadPortCard();
        const d = await api(`/api/instances/${state.inst.id}`);
        state.inst.server = d.server; renderInstanceHead();
      } catch (e) { toast(e.message, "err"); }
      finally { btn.disabled = false; }
    };
  } catch (e) {
    host.innerHTML = `<div class="banner err">Port info unavailable — ${esc(e.message)}</div>`;
  }
}

// --- full server.properties editor ------------------------------------

let propsState = { items: [], filter: "", changed: {} };

async function loadPropertiesEditor() {
  const host = $("#propsCard");
  if (!host) return;
  try {
    const d = await api(`/api/instances/${state.inst.id}/properties`);
    propsState = { items: d.items, filter: "", changed: {}, groups: d.groups,
                   count: d.count };
    renderPropertiesEditor();
  } catch (e) {
    host.innerHTML = `<div class="banner err">server.properties unavailable — ${esc(e.message)}</div>`;
  }
}

function propControl(i) {
  const id = "prop_" + i.key.replace(/[^\w]/g, "_");
  if (i.type === "bool") {
    return `<label class="switch">
        <input type="checkbox" class="propEdit" id="${id}" data-key="${esc(i.key)}"
               ${String(i.value).toLowerCase() === "true" ? "checked" : ""} aria-label="${esc(i.key)}">
        <span class="track"></span></label>`;
  }
  if (i.type === "enum" && i.choices) {
    return `<select class="custom-select propEdit" id="${id}" data-key="${esc(i.key)}" aria-label="${esc(i.key)}">
        ${i.choices.map((c) => `<option ${c === String(i.value) ? "selected" : ""}>${esc(c)}</option>`).join("")}
      </select>`;
  }
  if (i.type === "int") {
    return `<input type="number" class="propEdit" id="${id}" data-key="${esc(i.key)}"
              value="${esc(String(i.value))}" aria-label="${esc(i.key)}" style="width:120px">`;
  }
  return `<input type="text" class="propEdit" id="${id}" data-key="${esc(i.key)}"
            value="${esc(String(i.value))}" aria-label="${esc(i.key)}" style="width:100%;max-width:260px">`;
}

function renderPropertiesEditor() {
  const host = $("#propsCard");
  if (!host) return;
  const f = propsState.filter.toLowerCase();
  const items = propsState.items.filter(
    (i) => !f || i.key.toLowerCase().includes(f) ||
           (i.description || "").toLowerCase().includes(f));

  const byGroup = {};
  items.forEach((i) => (byGroup[i.group] = byGroup[i.group] || []).push(i));
  const dirty = Object.keys(propsState.changed).length;

  host.innerHTML = `
    <div class="card-head">
      <h3>server.properties</h3>
      <span class="pill mono">${propsState.count} keys</span>
      <div class="spacer"></div>
      <div class="search-input-wrap" style="max-width:210px">
        <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
        <input type="search" id="propFilter" placeholder="Filter keys" value="${esc(propsState.filter)}">
      </div>
      <button class="btn btn-sm btn-accent-solid" id="propsSave" ${dirty ? "" : "disabled"}>
        Save${dirty ? ` (${dirty})` : ""}</button>
    </div>
    ${Object.keys(byGroup).length ? Object.entries(byGroup).map(([group, list]) => `
      <div class="group">${esc(group)}</div>
      ${list.map((i) => `
        <div class="optrow propEdit ${i.guarded ? "guarded" : ""}">
          <div class="body">
            <div class="t"><code>${esc(i.key)}</code>
              ${i.absent ? '<span class="pill ghost">not set</span>' : ""}
              ${i.modified ? '<span class="pill warn">changed from default</span>' : ""}
              ${propsState.changed[i.key] !== undefined ? '<span class="pill ok">unsaved</span>' : ""}
              ${i.guarded ? '<span class="pill ghost">guarded</span>' : ""}</div>
            ${i.description ? `<div class="why">${esc(i.description)}</div>` : ""}
            ${i.guarded ? `<div class="why">${esc(i.guarded)}</div>` : ""}
          </div>
          <div class="control">${i.guarded
            ? `<span class="readonly-value">
                 <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M7 11V8a5 5 0 0 1 10 0v3M5 11h14v9H5z"></path></svg>
                 ${esc(String(i.value))}
               </span>`
            : propControl(i)}</div>
        </div>`).join("")}`).join("")
      : `<div class="empty"><div class="t">No keys match</div><div class="d">Nothing in server.properties matches that filter.</div></div>`}`;

  $("#propFilter", host).oninput = (e) => {
    propsState.filter = e.target.value;
    renderPropertiesEditor();
    const el = $("#propFilter", host);
    el.focus(); el.setSelectionRange(el.value.length, el.value.length);
  };

  $$(".propEdit", host).forEach((el) => {
    el.onchange = () => {
      const key = el.dataset.key;
      const value = el.type === "checkbox" ? el.checked : el.value;
      propsState.changed[key] = value;
      const item = propsState.items.find((i) => i.key === key);
      if (item) item.value = el.type === "checkbox"
        ? (el.checked ? "true" : "false") : el.value;
      const save = $("#propsSave", host);
      save.disabled = false;
      save.textContent = `Save (${Object.keys(propsState.changed).length})`;
      const row = el.closest(".optrow");
      if (row && !$(".pill.ok", row)) {
        const t = $(".t", row);
        if (t) t.insertAdjacentHTML("beforeend", ' <span class="pill ok">unsaved</span>');
      }
    };
  });

  $("#propsSave", host).onclick = async () => {
    const btn = $("#propsSave", host);
    btn.disabled = true;
    try {
      const res = await api(`/api/instances/${state.inst.id}/properties`, {
        method: "POST", body: { updates: propsState.changed },
      });
      const n = Object.keys(res.saved || {}).length;
      if (n) toast(`Saved ${n} propert${n === 1 ? "y" : "ies"} — restart to apply`, "ok");
      (res.rejected || []).forEach((r) => toast(`${r.key}: ${r.why}`, "err"));
      propsState.changed = {};
      loadPropertiesEditor();
    } catch (e) { toast(e.message, "err"); btn.disabled = false; }
  };
}

function renderOptimize(p) {
  const h = p.host, m = p.memory;
  const total = h.total_ram_gb || 0;
  const pct = total ? Math.min(100, (m.heap_gb / total) * 100) : 0;
  const ceilPct = total ? Math.min(100, (m.ceiling_gb / total) * 100) : 0;
  const groups = {};
  p.flags.forEach((f, i) => (groups[f.group] = groups[f.group] || []).push({ ...f, i }));
  const activeFlags = p.flags.filter((f) => f.enabled).length;
  const pendingProps = p.properties.filter((prop) => prop.enabled && !prop.applied).length;
  const overCeiling = m.heap_gb > m.ceiling_gb;

  $("#optOut").innerHTML = `
    <div class="opt-layout">
      <nav class="opt-nav" id="optNav" aria-label="Optimizer sections">
        <button class="opt-nav-btn" data-opt="host" aria-current="true"><span class="k">Host</span><span class="b">${esc(String(h.total_ram_gb ?? "?"))} GB</span></button>
        <button class="opt-nav-btn" data-opt="heap"><span class="k">Heap</span><span class="b">${esc(String(m.heap_gb))} GB</span></button>
        <button class="opt-nav-btn" data-opt="flags"><span class="k">JVM flags</span><span class="b">${activeFlags}</span></button>
        <button class="opt-nav-btn" data-opt="props"><span class="k">Properties</span><span class="b">${pendingProps || p.properties.length}</span></button>
        <button class="opt-nav-btn" data-opt="port"><span class="k">Port</span><span class="b" id="optNavPort">—</span></button>
        <div class="opt-nav-note">Changes apply on next restart.</div>
        <button class="btn btn-primary" id="optApply">Apply Profile</button>
      </nav>

      <div class="opt-sections">
        <!-- host -->
        <div class="card" data-opt-sec="host">
          <span class="section-label">Host hardware</span>
          <div class="statgrid">
            <div class="stat"><div class="k">Total RAM</div><div class="v">${esc(String(h.total_ram_gb))} GB</div></div>
            <div class="stat"><div class="k">Available</div><div class="v ok">${esc(String(h.available_ram_gb))} GB</div></div>
            <div class="stat"><div class="k">CPU cores</div><div class="v">${esc(String(h.cpu_count))}</div></div>
            <div class="stat"><div class="k">Installed mods</div><div class="v">${esc(String(p.mod_count))}</div></div>
          </div>
          ${h.cpu_model ? `<div class="faint mono" style="margin-top:11px">${esc(h.cpu_model)}</div>` : ""}
          ${h.note ? `<div class="banner" style="margin-top:12px"><span>${esc(h.note)}</span></div>` : ""}
        </div>

        <!-- heap -->
        <div class="card" data-opt-sec="heap">
          <div class="row" style="align-items:flex-end">
            <div>
              <span class="section-label">Heap allocation</span>
              <div class="row tight">
                <input type="number" id="optHeap" class="input-num" value="${esc(String(m.heap_gb))}"
                       min="1" max="128" step="0.5" style="width:88px" aria-label="Heap in GB">
                <span class="muted">GB of ${esc(String(total))} GB host RAM</span>
              </div>
            </div>
            <div style="flex:1;min-width:220px">
              <div class="meter">
                <div style="width:${pct}%"></div>
                <span class="tick" style="left:${ceilPct}%"></span>
                <span>${esc(String(m.heap_gb))} GB heap</span>
              </div>
              <div class="meter-legend">
                <span>Safe ceiling ${esc(String(m.ceiling_gb))} GB · reserve ${esc(String(m.reserve_gb))} GB</span>
                <span>Pack asks ${esc(String(m.requested_gb))} GB</span>
              </div>
            </div>
          </div>
          <div class="faint" style="margin-top:12px">Recommendation based on ${esc(m.basis)}.</div>
          ${overCeiling ? `<div class="banner" style="margin:12px 0 0">
              <span class="mono" style="color:var(--amber-ink);font-weight:700">!</span>
              <span>Above the safe ceiling. The host keeps ${esc(String(m.reserve_gb))} GB for itself;
                going higher risks the OOM killer taking the server mid-session.</span>
            </div>` : ""}
          ${(m.warnings || []).map((w) => `<div class="banner" style="margin-top:12px"><span>${esc(w)}</span></div>`).join("")}
          ${p.current.exists ? `<div class="faint mono" style="margin-top:12px">
              Currently applied: -Xms${esc(String(p.current.xms_mb))}M -Xmx${esc(String(p.current.xmx_mb))}M · ${p.current.flags.length} flags
            </div>` : ""}
          ${p.note ? `<div class="banner" style="margin-top:12px"><span>${esc(p.note)}</span></div>` : ""}
        </div>

        <!-- JVM flags -->
        <div class="card flush" data-opt-sec="flags">
          <div class="card-head">
            <h3>Aikar's JVM flags</h3>
            <span class="pill ok">${activeFlags} active</span>
            <div class="spacer"></div>
            <button class="btn btn-sm btn-secondary" id="optAll">Select All</button>
            <button class="btn btn-sm btn-secondary" id="optNone">None</button>
            <button class="btn btn-sm btn-accent" id="optRec">Reset to Recommended</button>
          </div>
          ${Object.entries(groups).map(([group, items]) => `
            <div class="group">${esc(group)}</div>
            ${items.map((f) => `
              <div class="optrow">
                <label class="switch">
                  <input type="checkbox" class="flagSel" data-flag="${esc(f.flag)}" data-rec="${f.recommended}"
                         ${f.enabled ? "checked" : ""} aria-label="${esc(f.label)}">
                  <span class="track"></span>
                </label>
                <div class="body">
                  <div class="t">${esc(f.label)} ${f.applied ? '<span class="pill ok">Active</span>' : ""}</div>
                  <div class="why">${esc(f.why)}</div>
                </div>
                <code class="flagname">${esc(f.flag)}</code>
              </div>`).join("")}`).join("")}
        </div>

        <!-- property presets -->
        <div class="card flush" data-opt-sec="props">
          <div class="card-head">
            <h3>Server properties optimization</h3>
            ${pendingProps ? `<span class="pill warn">${pendingProps} pending</span>`
                           : '<span class="pill ok">all applied</span>'}
          </div>
          ${p.properties.map((prop) => `
            <div class="optrow">
              <label class="switch">
                <input type="checkbox" class="propSel" data-key="${esc(prop.key)}" data-value="${esc(prop.value)}"
                       ${prop.enabled ? "checked" : ""} aria-label="Tune ${esc(prop.key)}">
                <span class="track"></span>
              </label>
              <div class="body">
                <div class="t"><code>${esc(prop.key)} = ${esc(prop.value)}</code>
                  ${prop.applied ? '<span class="pill ok">Active</span>'
                    : prop.current != null ? `<span class="pill warn">Current: ${esc(prop.current)}</span>` : ""}</div>
                <div class="why">${esc(prop.why)}</div>
              </div>
            </div>`).join("")}
          <div class="apply-bar">
            <span class="faint">Takes effect on next restart.</span>
            <div class="spacer"></div>
            <button class="btn btn-primary" data-opt-apply>Apply Optimization Profile</button>
          </div>
        </div>

        <div class="card" id="portCard" data-opt-sec="port">
          <div class="loading-line"><span class="spin"></span> Reading port assignment…</div>
        </div>
        <div class="card flush" id="propsCard">
          <div class="skel-row"><span class="skel skel-line w40"></span></div>
          <div class="skel-row"><span class="skel skel-line w60"></span></div>
        </div>
      </div>
    </div>`;

  loadPortCard();
  loadPropertiesEditor();
  bindOptNav();

  $("#optAll").onclick = () => $$(".flagSel").forEach((c) => (c.checked = true));
  $("#optNone").onclick = () => $$(".flagSel").forEach((c) => (c.checked = false));
  $("#optRec").onclick = () => $$(".flagSel").forEach((c) => (c.checked = c.dataset.rec === "true"));

  const applyProfile = async (btn) => {
    const buttons = [$("#optApply"), ...$$("[data-opt-apply]")].filter(Boolean);
    buttons.forEach((b) => (b.disabled = true));
    const flags = $$(".flagSel:checked").map((c) => c.dataset.flag);
    const properties = {};
    $$(".propSel:checked").forEach((c) => (properties[c.dataset.key] = c.dataset.value));
    try {
      const res = await api(`/api/instances/${state.inst.id}/optimize`, {
        method: "POST",
        body: {
          heap_gb: Number($("#optHeap").value),
          flags,
          properties,
          xms_equals_xmx: true,
        },
      });
      res.applied.forEach((a) => toast(a, "ok"));
      (res.skipped || []).forEach((sk) => toast(`${sk.what}: ${sk.why}`, "err"));
      loadOptimize();
    } catch (e) {
      toast(e.message, "err");
      buttons.forEach((b) => (b.disabled = false));
    }
  };

  $("#optApply").onclick = () => applyProfile();
  $$("[data-opt-apply]").forEach((b) => (b.onclick = () => applyProfile()));
}

// Five tools in one tab: the nav scrolls to a section and follows the scroll
// position back. Every section stays in the DOM -- Apply writes heap, flags and
// properties in one request, so the user has to be able to see all of it.
function bindOptNav() {
  const nav = $("#optNav");
  if (!nav) return;
  const sections = $$("[data-opt-sec]");

  $$(".opt-nav-btn", nav).forEach((b) => {
    b.onclick = () => {
      const target = sections.find((sec) => sec.dataset.optSec === b.dataset.opt);
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
      setOptCurrent(b.dataset.opt);
    };
  });

  if (state.optObserver) state.optObserver.disconnect();
  if (!("IntersectionObserver" in window)) return;
  state.optObserver = new IntersectionObserver((entries) => {
    const visible = entries.filter((e) => e.isIntersecting)
      .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
    if (visible) setOptCurrent(visible.target.dataset.optSec);
  }, { rootMargin: "-96px 0px -60% 0px" });
  sections.forEach((sec) => state.optObserver.observe(sec));
}

function setOptCurrent(name) {
  $$("#optNav .opt-nav-btn").forEach((b) => {
    if (b.dataset.opt === name) b.setAttribute("aria-current", "true");
    else b.removeAttribute("aria-current");
  });
}

// --- Modpack Tab -------------------------------------------------------

async function loadPackTab() {
  const i = state.inst;
  const pack = i.manifest.pack;
  const host = $("#packOut");
  if (!pack) {
    host.innerHTML = `
      <div class="empty">
        <div class="t">No pack metadata</div>
        <div class="d">This instance was not installed by BlessForge, so there is no manifest to read.
          Mods, configs and tuning still work.</div>
      </div>`;
    return;
  }
  // A private pack has no catalogue entry and therefore no release list: the
  // way to update one is to export it again and re-import, so that is what
  // the button offers instead of a version table it could never fill.
  const imported = pack.source === "upload";
  const excluded = (i.manifest.excluded_mods || []).length;
  const problems = (i.manifest.problems || []).length;
  const overrides = i.manifest.override_files || (pack.summary && pack.summary.override_files);

  host.innerHTML = `
    <div class="card" style="max-width:900px">
      <div class="row">
        <h2 style="margin:0">${esc(pack.name || "")}</h2>
        ${imported
          ? `<span class="pill warn">Imported pack</span>`
          : `<span class="pill ok">${esc(pack.version || "?")} installed</span>`}
        <span class="pill">${esc(pack.install_source === "server_pack" ? "Official Server Pack" : "Assembled Manifest")}</span>
        <span class="pill mono">${esc(i.loader || "")} · ${esc(i.minecraft || "")}</span>
      </div>
      ${imported && pack.archive ? `<div class="mono faint" style="margin-top:6px">${esc(pack.archive)}</div>` : ""}
      <div class="summary-note ${imported ? "warn" : ""}" style="margin-top:14px">
        <span class="glyph">${imported ? "!" : "i"}</span>
        <span>${imported
          ? "This instance came from an export you made yourself, so there are no catalogue releases to move between. To update it, export the profile again and drop the new zip in — the world, name and port are kept."
          : "Installed from the CurseForge catalogue. Switching release re-downloads the mod set and keeps the world."}</span>
      </div>
      <div class="statgrid" style="margin-top:14px">
        <div class="stat"><div class="k">Client mods excluded</div><div class="v">${excluded}</div></div>
        <div class="stat"><div class="k">Failed downloads</div>
          <div class="v ${problems ? "warn" : ""}">${problems}</div>
          ${problems ? '<div class="sub">see Troubleshoot</div>' : ""}</div>
        ${overrides != null ? `<div class="stat"><div class="k">Override files</div><div class="v">${esc(String(overrides))}</div></div>` : ""}
      </div>
      <button class="btn btn-primary" id="switchPack" style="margin-top:16px">${imported ? "Re-import an Updated Export" : "Switch Modpack Release Version"}</button>
      <div id="packVersions" style="margin-top:14px"></div>
    </div>`;

  if (imported) {
    $("#switchPack").onclick = () =>
      openImportModal({
        serverId: i.id,
        serverName: (i.server && i.server.server_name) || pack.name || "this instance",
      });
    return;
  }

  $("#switchPack").onclick = async () => {
    const box = $("#packVersions");
    box.innerHTML = `<div class="loading-line"><span class="spin"></span> Loading release history…</div>`;
    try {
      const r = await api(`/api/modpacks/${pack.project_id}/files?page_size=50`);
      box.innerHTML = `
        <div class="table-wrap">
          <table>
            <thead><tr><th>Release</th><th>MC</th><th>Source</th><th></th></tr></thead>
            <tbody>
              ${r.items.map((f) => {
                const current = String(f.file_id) === String(pack.file_id);
                return `
                  <tr class="${current ? "active" : ""}">
                    <td>${esc(f.display_name)}</td>
                    <td class="mono">${esc((f.game_versions || []).join(", "))}</td>
                    <td>${f.server_pack_file_id
                      ? '<span class="pill ok">Server Pack</span>'
                      : '<span class="pill">Assembled Manifest</span>'}</td>
                    <td style="text-align:right">${current
                      ? '<span class="pill">Installed</span>'
                      : `<button class="btn btn-sm btn-secondary" data-pk="${esc(String(f.file_id))}">Switch</button>`}</td>
                  </tr>`;
              }).join("")}
            </tbody>
          </table>
        </div>`;

      $$("[data-pk]").forEach((b) => {
        b.onclick = async () => {
          const ok = await confirmDialog({
            title: "Switch modpack release",
            message: "Update this instance to the selected release? Your world data is preserved, but mod jars and pack configs are replaced to match it.",
            confirmLabel: "Switch version",
          });
          if (!ok) return;
          b.disabled = true;
          try {
            const res = await api(`/api/instances/${i.id}/switch-pack-version`, {
              method: "POST",
              body: { mod_id: pack.project_id, file_id: Number(b.dataset.pk) },
            });
            followJob(res.job_id, "Switching Modpack Version", () => openInstance(i.id));
          } catch (e) {
            toast(e.message, "err");
            b.disabled = false;
          }
        };
      });
    } catch (e) {
      box.innerHTML = `<div class="banner err"><span>${esc(e.message)}</span></div>`;
    }
  };
}

// --- Browse & Install Modpacks -----------------------------------------

async function searchPacks(reset = true) {
  if (reset) {
    state.packIndex = 0;
    $("#packs").innerHTML = `<div class="skel skel-card"></div><div class="skel skel-card"></div>
                             <div class="skel skel-card"></div><div class="skel skel-card"></div>`;
  }
  const params = new URLSearchParams({
    q: $("#q").value.trim(),
    sort: $("#sort").value,
    index: state.packIndex,
    page_size: 30,
  });
  if ($("#mcv").value) params.set("game_version", $("#mcv").value);
  if ($("#loader").value) params.set("loader", $("#loader").value);
  $("#searchBtn").disabled = true;
  try {
    const r = await api("/api/browse/modpacks?" + params);
    const host = $("#packs");
    if (reset) host.innerHTML = "";

    const installed = new Set(state.instances
      .map((i) => i.pack && i.pack.project_id)
      .filter(Boolean)
      .map(String));

    r.items.forEach((p) => {
      const el = document.createElement("article");
      el.className = "pack" + (installed.has(String(p.id)) ? " already-installed" : "");
      el.tabIndex = 0;
      el.dataset.open = String(p.id);
      const vers = [...new Set((p.latest_files || []).flatMap((f) => f.game_versions || []))].slice(0, 3);
      const loaders = [...new Set((p.latest_files || []).flatMap((f) => f.loaders || []))].slice(0, 1);
      const hasServerPack = (p.latest_files || []).some((f) => f.server_pack_file_id);
      el.innerHTML = `
        <div class="pack-head">
          ${iconHTML(p.logo, p.name, "mod-icon")}
          <div class="body">
            <div class="title">${esc(p.name)}</div>
            <div class="downloads">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M12 4v12m0 0l-5-5m5 5l5-5M4 20h16"></path></svg>
              ${num(p.downloads)}
            </div>
          </div>
        </div>
        <p class="summary">${esc(p.summary || "")}</p>
        <div class="meta">
          ${vers.map((v) => `<span class="pill mono">${esc(v)}</span>`).join("")}
          ${loaders.map((l) => `<span class="pill mono">${esc(l)}</span>`).join("")}
          ${installed.has(String(p.id)) ? '<span class="pill ok">Installed</span>'
            : hasServerPack ? '<span class="pill ok">Server Pack</span>' : ""}
        </div>`;
      el.onclick = () => openPack(p);
      el.onkeydown = (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openPack(p); }
      };
      host.appendChild(el);
    });

    state.packIndex += r.items.length;
    $("#morePacks").classList.toggle("hidden", r.items.length < 30);
    if (!r.items.length && reset) {
      host.innerHTML = `
        <div class="empty" style="grid-column:1/-1">
          ${FOX_MARK}
          <div class="t">No modpacks matched</div>
          <div class="d">Nothing in the CurseForge catalogue matches these filters. If the pack is
            one you built yourself it will never appear here — import its export instead.</div>
          <div class="actions"><button class="btn btn-primary" id="emptySearchImport">Import a pack export</button></div>
        </div>`;
      const b = $("#emptySearchImport");
      if (b) b.onclick = () => openImportModal();
    }
  } catch (e) {
    toast(e.message, "err");
    if (reset) {
      $("#packs").innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="t">Search failed</div><div class="d">${esc(e.message)}</div></div>`;
    }
  } finally {
    $("#searchBtn").disabled = false;
  }
}

async function openPack(pack) {
  const m = modal({
    title: pack.name,
    wide: true,
    body: `
      <div class="row" style="align-items:flex-start">
        ${iconHTML(pack.logo, pack.name, "mod-icon")}
        <div style="flex:1;min-width:0">
          <p class="muted" style="margin:0 0 8px">${esc(pack.summary || "")}</p>
          <div class="row tight">
            <span class="pill mono">${num(pack.downloads)} downloads</span>
            ${pack.url ? `<a href="${esc(pack.url)}" target="_blank" rel="noopener" class="btn btn-sm btn-ghost">View on CurseForge ↗</a>` : ""}
          </div>
        </div>
      </div>
      <span class="section-label" style="margin-top:16px">Available releases</span>
      <div id="verList"><div class="loading-line"><span class="spin"></span> Loading releases…</div></div>`,
  });

  try {
    const r = await api(`/api/modpacks/${pack.id}/files?page_size=50`);
    $("#verList", m.el).innerHTML = `
      <div class="table-wrap">
        <table>
          <thead><tr><th>Release</th><th>MC</th><th>Loader</th><th>Channel</th><th>Source</th><th></th></tr></thead>
          <tbody>
            ${r.items.map((f, idx) => `
              <tr>
                <td>${esc(f.display_name)}</td>
                <td class="mono">${esc((f.game_versions || []).join(", "))}</td>
                <td class="mono">${esc((f.loaders || []).join(", "))}</td>
                <td><span class="pill ${f.release_type === "release" ? "ok" : "warn"}">${esc(f.release_type)}</span></td>
                <td>${f.server_pack_file_id
                  ? '<span class="pill ok">Server Pack</span>'
                  : '<span class="pill">Assembled Manifest</span>'}</td>
                <td style="text-align:right">
                  <button class="btn btn-sm btn-primary" data-i="${idx}">Install</button>
                </td>
              </tr>`).join("")}
          </tbody>
        </table>
      </div>`;

    $$("[data-i]", m.el).forEach((b) => {
      b.onclick = () => {
        m.close();
        installWizard(pack, r.items[Number(b.dataset.i)]);
      };
    });
  } catch (e) {
    $("#verList", m.el).innerHTML = `<div class="banner err"><span>${esc(e.message)}</span></div>`;
  }
}

function installWizard(pack, file) {
  // The wizard is shared by both sources. An imported archive has an
  // upload_id and no catalogue ids; everything downstream of Continue is
  // identical, so only the header and the source row differ.
  const imported = !!file.upload_id;
  const sum = (pack.upload && pack.upload.summary) || null;

  // The only moment the user gets to check the archive is the one they meant,
  // before a multi-minute install starts.
  const summaryHTML = sum ? `
      <div class="statgrid">
        <div class="stat">
          <div class="k">Runtime</div>
          <div class="v">${esc(sum.loader || "?")} ${esc(sum.loader_version || "")}</div>
          <div class="sub">Minecraft ${esc(sum.minecraft || "?")}</div>
        </div>
        <div class="stat">
          <div class="k">Mods</div>
          <div class="v">${num((sum.manifest_mods || 0) + (sum.override_jars || 0))}</div>
          ${sum.override_jars ? `<div class="sub">${num(sum.override_jars)} bundled jars</div>` : ""}
        </div>
        <div class="stat"><div class="k">Override files</div><div class="v">${num(sum.override_files || 0)}</div></div>
        <div class="stat">
          <div class="k">Pack asks for</div>
          <div class="v">${sum.recommended_ram_mb ? (sum.recommended_ram_mb / 1024).toFixed(1) + " GB" : "—"}</div>
        </div>
      </div>
      ${(sum.warnings || []).map((w) => `
        <div class="summary-note warn" style="margin-top:12px"><span class="glyph">!</span><span>${esc(w)}</span></div>`).join("")}` : "";

  const m = modal({
    title: (imported ? "Install Imported Pack — " : "Install Modpack — ") + pack.name,
    body: `
      ${imported ? "" : `<span class="pill mono" style="margin-bottom:14px;display:inline-block">${esc(file.display_name)}</span>`}
      ${summaryHTML}

      <div class="row" style="margin-top:16px">
        <div style="flex:1;min-width:200px">
          <span class="section-label">Server name</span>
          <input type="text" id="iName" value="${esc(pack.name.slice(0, 60))}" style="width:100%">
        </div>
        <div>
          <span class="section-label">Port</span>
          <input type="number" id="iPort" class="input-num" value="25565" style="width:112px">
        </div>
      </div>

      <div style="display:grid;gap:10px;margin-top:16px">
        <label class="custom-checkbox">
          <input type="checkbox" id="iServerPack" ${imported ? "disabled" : (file.server_pack_file_id ? "checked" : "disabled")}>
          <span class="checkbox-mark"></span>
          <span class="checkbox-label">${imported
            ? (sum && sum.kind === "server_pack"
                ? "Imported archive is already a server pack — installed as-is"
                : "Imported export assembled from its manifest (mods fetched from CurseForge)")
            : (file.server_pack_file_id
                ? "Prefer the official server pack build"
                : "No official server build — assembled from the manifest")}</span>
        </label>

        <label class="custom-checkbox">
          <input type="checkbox" id="iReview" checked>
          <span class="checkbox-mark"></span>
          <span class="checkbox-label">Review &amp; strip client-only mods before installing</span>
        </label>

        <label class="custom-checkbox">
          <input type="checkbox" id="iOptimize" checked>
          <span class="checkbox-mark"></span>
          <span class="checkbox-label">Auto-tune JVM flags and heap for this host</span>
        </label>
      </div>`,
    actions: [
      { label: "Cancel", onClick: (mm) => mm.close() },
      { label: "Continue", cls: "primary", onClick: async (mm, btn) => {
          btn.disabled = true;
          const opts = {
            ...(imported
              ? { upload_id: file.upload_id }
              : { mod_id: pack.id, file_id: file.file_id,
                  prefer_server_pack: $("#iServerPack", mm.el).checked }),
            server_name: $("#iName", mm.el).value.trim() || pack.name,
            port: Number($("#iPort", mm.el).value) || 25565,
            optimize: $("#iOptimize", mm.el).checked,
          };
          const wantsReview = $("#iReview", mm.el).checked;
          mm.close();
          if (wantsReview) startPreflight(pack, opts);
          else startInstall(opts, pack.name);
        } },
    ],
  });
}

// --- Import a CurseForge profile export --------------------------------
//
// A pack the user assembled themselves is never published, so it has no
// project id and cannot be found by searching. The archive the CurseForge
// app writes is the only handle on it, so it is uploaded once, kept, and
// then travels the same preflight -> review -> install road as a catalogue
// pack.

function uploadArchive(file, onProgress) {
  // XMLHttpRequest rather than fetch(): fetch still cannot report upload
  // progress, and these archives run to hundreds of megabytes -- a dialog
  // that sits at "please wait" for four minutes reads as frozen.
  const xhr = new XMLHttpRequest();
  const promise = new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file, file.name);
    xhr.open("POST", "/api/uploads/modpack");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total, e.loaded, e.total);
    };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status >= 200 && xhr.status < 300 && data) resolve(data);
      else reject(new Error((data && data.detail) || `upload failed (HTTP ${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error("the upload connection dropped"));
    xhr.onabort = () => reject(new Error("Upload cancelled"));
    xhr.send(form);
  });
  return { promise, abort: () => xhr.abort() };
}

function importSummaryLine(record) {
  const s = record.summary || {};
  const bits = [];
  if (s.loader) bits.push(`${s.loader} ${s.minecraft || ""}`.trim());
  const mods = (s.manifest_mods || 0) + (s.override_jars || 0);
  if (mods) bits.push(`${mods} mods`);
  if (s.override_files) bits.push(`${num(s.override_files)} override files`);
  bits.push(mb(record.size));
  return bits.filter(Boolean).join(" · ");
}

// Turns an upload record into the (pack, file) pair the install wizard takes.
function importAsPack(record) {
  const s = record.summary || {};
  const name = s.name || record.file_name.replace(/\.zip$/i, "");
  return [
    { id: null, name, logo: null, summary: "", upload: record },
    { display_name: record.file_name, file_id: null,
      server_pack_file_id: null, upload_id: record.upload_id },
  ];
}

function openImportModal({ serverId = null, serverName = "" } = {}) {
  const reimport = !!serverId;
  let busy = null;

  const m = modal({
    title: reimport ? `Re-import pack for ${serverName}` : "Import a CurseForge export",
    wide: true,
    body: `
      ${reimport ? `
        <div class="summary-note info" style="margin-bottom:14px">
          <span class="glyph">i</span>
          <span>Re-importing <strong>${esc(serverName)}</strong>. The world, server name and port are
            kept; only mods and overrides are replaced. You will see the review step before
            anything is written.</span>
        </div>` : ""}
      <div class="dropzone" id="impDrop" tabindex="0" role="button"
           aria-label="Choose a modpack export to upload">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
          <path d="M12 16V4m0 0L7 9m5-5l5 5M4 17v3h16v-3"></path>
        </svg>
        <div class="dz-title">Drop a CurseForge export here</div>
        <div class="dz-sub">or click to choose a <code>.zip</code> — up to about 1 GB</div>
        <input type="file" id="impFile" accept=".zip,application/zip,application/x-zip-compressed" class="hidden">
      </div>

      <div class="dz-progress hidden" id="impProgress">
        <div class="file-row">
          <span class="fname" id="impName"></span>
          <span class="pct" id="impPct">0%</span>
        </div>
        <div class="bar" role="progressbar" aria-label="Upload progress"
             aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" id="impBarWrap">
          <div id="impBar"></div>
        </div>
        <div class="stage" id="impStage">Uploading…</div>
      </div>

      <details class="import-help">
        <summary>How do I make an export?</summary>
        <ol>
          <li>Open the CurseForge app and go to <strong>My Modpacks</strong>.</li>
          <li>Click the <strong>…</strong> menu on your profile → <strong>Create Profile Export</strong>.</li>
          <li>Leave <strong>Mods</strong>, <strong>Config</strong> and any script folders ticked
            (KubeJS, Open Loader). Client-only folders like <code>shaderpacks</code> are dropped
            here anyway, so ticking them does no harm.</li>
          <li>Export, then drop the saved <code>.zip</code> above.</li>
        </ol>
      </details>

      <div id="impRecent"></div>`,
    actions: [{ label: "Cancel", onClick: (mm) => { if (busy) busy.abort(); mm.close(); } }],
    onClose: () => { if (busy) busy.abort(); },
  });

  const drop = $("#impDrop", m.el);
  const input = $("#impFile", m.el);
  const progress = $("#impProgress", m.el);
  const bar = $("#impBar", m.el);
  const barWrap = $("#impBarWrap", m.el);
  const pct = $("#impPct", m.el);
  const stage = $("#impStage", m.el);

  const setPct = (fraction) => {
    const p = Math.round(fraction * 100);
    bar.style.width = p + "%";
    pct.textContent = p + "%";
    barWrap.setAttribute("aria-valuenow", String(p));
  };

  const proceed = (record) => {
    m.close();
    const [pack, file] = importAsPack(record);
    if (reimport) {
      // Re-importing an existing instance still goes through the review, so
      // an export that picked up a client mod cannot quietly break a server
      // that was working ten minutes ago.
      startPreflight(pack, { upload_id: record.upload_id, switch_server_id: serverId });
    } else {
      installWizard(pack, file);
    }
  };

  const send = async (file) => {
    if (!file) return;
    if (!/\.zip$/i.test(file.name)) {
      m.error("That is not a .zip. Export the profile from the CurseForge app first.");
      return;
    }
    m.error("");
    drop.classList.add("hidden");
    progress.classList.remove("hidden");
    const nameEl = $("#impName", m.el);
    if (nameEl) nameEl.textContent = file.name;
    stage.textContent = `Uploading ${file.name}…`;
    setPct(0);
    busy = uploadArchive(file, (f) => {
      setPct(f);
      // The server reads the archive once the bytes land; say so, because
      // that pause is long for a big pack and 100% with no movement looks
      // like a stall.
      if (f >= 1) {
        stage.innerHTML = `<span class="spin"></span> Reading the archive — opening the zip and parsing manifest.json…`;
      }
    });
    try {
      const record = await busy.promise;
      busy = null;
      proceed(record);
    } catch (e) {
      busy = null;
      progress.classList.add("hidden");
      drop.classList.remove("hidden");
      m.error(e.message);
    }
  };

  drop.onclick = (e) => { if (e.target !== input) input.click(); };
  drop.onkeydown = (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); }
  };
  input.onchange = () => send(input.files && input.files[0]);

  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.add("dragging");
    })
  );
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.remove("dragging");
    })
  );
  drop.addEventListener("drop", (e) => {
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    send(f);
  });

  loadRecentImports(m, proceed);
  return m;
}

// Archives already on the server. Re-installing a 900 MB export should not
// mean uploading it a second time.
async function loadRecentImports(m, proceed) {
  const host = $("#impRecent", m.el);
  let r;
  try {
    r = await api("/api/uploads");
  } catch {
    return;
  }
  if (!r.items || !r.items.length) return;

  host.innerHTML = `
    <span class="section-label" style="margin-top:18px">Already imported</span>
    <div class="faint" style="margin-bottom:10px">Kept on the server so you can install again
      without re-uploading. The oldest are removed once there are more than ${r.limit}.</div>
    <div class="summary-list">
      ${r.items.map((it) => `
        <div class="import-row">
          <div class="body">
            <div class="flagname">${esc((it.summary && it.summary.name) || it.file_name)}</div>
            <div class="fname">${esc(it.file_name)}</div>
            <div class="sum">${esc(importSummaryLine(it))}</div>
          </div>
          <button class="btn btn-sm btn-primary" data-imp="${esc(it.upload_id)}">Use this</button>
          <button class="btn btn-sm btn-ghost btn-icon" data-impdel="${esc(it.upload_id)}"
                  title="Delete this archive" aria-label="Delete this archive">${DELETE_ICON}</button>
        </div>`).join("")}
    </div>`;

  const byId = Object.fromEntries(r.items.map((it) => [it.upload_id, it]));
  $$("[data-imp]", host).forEach((b) => {
    b.onclick = () => proceed(byId[b.dataset.imp]);
  });
  $$("[data-impdel]", host).forEach((b) => {
    b.onclick = async () => {
      const id = b.dataset.impdel;
      const ok = await confirmDialog({
        title: "Delete imported archive",
        message: "Remove this archive from the server? Instances already installed from it are untouched, but re-installing means uploading it again.",
        confirmLabel: "Delete archive",
        danger: true,
      });
      if (!ok) return;
      b.disabled = true;
      try {
        await api(`/api/uploads/${id}`, { method: "DELETE" });
        b.closest(".import-row").remove();
        toast("Archive deleted", "ok");
      } catch (e) {
        m.error(e.message);
        b.disabled = false;
      }
    };
  });
}

async function startPreflight(pack, opts) {
  try {
    const r = await api("/api/install/preflight", {
      method: "POST",
      body: opts.upload_id
        ? { upload_id: opts.upload_id }
        : {
            mod_id: opts.mod_id,
            file_id: opts.file_id,
            prefer_server_pack: opts.prefer_server_pack,
          },
    });
    followJob(r.job_id, "Analysing pack — " + pack.name, (d) => {
      if (d.result) showReview(pack, opts, d.result);
    }, { auto: true });
  } catch (e) {
    toast(e.message, "err");
  }
}

function showReview(pack, opts, analysis) {
  const review = analysis.review || {};
  const items = review.candidates || [];
  const mem = analysis.memory || {};

  const rows = items.map((c, i) => {
    const keep = c.recommendation === "keep";
    const remove = c.recommendation === "remove";
    const tone = remove ? "remove" : keep ? "keep" : "review";
    return `
      <div class="reviewrow ${tone} ${c.bundled ? "bundled" : ""}">
        <label class="custom-checkbox">
          <input type="checkbox" class="rvSel" data-i="${i}" data-file="${esc(c.file_name)}"
                 ${remove ? "checked" : ""} ${keep ? "disabled" : ""}
                 aria-label="${keep ? "Required" : "Exclude"} ${esc(c.name || c.file_name)}">
          <span class="checkbox-mark"></span>
        </label>
        ${iconHTML(c.logo, c.name || c.file_name)}
        <div class="body">
          <div class="t">${esc(c.name || c.file_name)}
            ${c.bundled ? '<span class="pill ghost">bundled</span>' : ""}
            ${remove ? '<span class="pill warn">Client-Only</span>' : ""}
            ${c.recommendation === "review" ? '<span class="pill info">Review Needed</span>' : ""}
            ${keep ? '<span class="pill ok">Keep — Dependent Present</span>' : ""}
          </div>
          <div class="reasons">${esc((c.reasons || []).join(" · "))}</div>
          ${(c.required_by_others || []).length
            ? `<div class="reasons">Required by <strong>${esc(c.required_by_others.join("</strong>, <strong>"))}</strong></div>`
            : ""}
          <div class="jar">${esc(c.file_name)}</div>
        </div>
      </div>`;
  }).join("");

  modal({
    title: "Review client-only mods",
    wide: true,
    body: `
      <div class="statgrid">
        <div class="stat"><div class="k">Modpack</div><div class="v">${esc(analysis.pack.name || pack.name)}</div></div>
        <div class="stat"><div class="k">Runtime</div><div class="v">${esc(analysis.loader)}</div><div class="sub">Minecraft ${esc(analysis.minecraft)}</div></div>
        <div class="stat"><div class="k">Total mods</div><div class="v">${review.total_mods ?? "—"}</div></div>
        <div class="stat"><div class="k">Tuned heap</div><div class="v">${mem.heap_gb ?? "—"} GB</div></div>
      </div>

      ${[...(mem.warnings || []), ...(analysis.warnings || [])].map((w) => `
        <div class="summary-note warn" style="margin-top:12px"><span class="glyph">!</span><span>${esc(w)}</span></div>`).join("")}

      <span class="section-label" style="margin-top:16px">Candidate client-only mods (${items.length})</span>
      <div class="faint" style="margin-bottom:12px">
        Checked mods are excluded from the server install. Rows marked <em>Keep</em> are held back
        because another server-side mod requires them.
      </div>
      <div class="card flush">
        ${items.length ? rows : `
          <div class="empty"><div class="t">Nothing to strip</div>
            <div class="d">No client-only jars were detected in this pack.</div></div>`}
      </div>`,
    actions: [
      { label: "Cancel", onClick: (mm) => mm.close() },
      { label: "Proceed & Install", cls: "primary", onClick: (mm, btn) => {
          btn.disabled = true;
          const exclude = $$(".rvSel:checked", mm.el).map((c) => c.dataset.file);
          mm.close();
          startInstall({ ...opts, exclude_files: exclude }, pack.name);
        } },
    ],
  });
}

async function startInstall(opts, label) {
  // `switch_server_id` means "rebuild this existing instance from the pack"
  // rather than "create a new one" -- the re-import path for a private pack,
  // which keeps the world and the server id.
  const target = opts.switch_server_id;
  const body = { ...opts };
  delete body.switch_server_id;
  try {
    const r = await api(
      target ? `/api/instances/${target}/switch-pack-version` : "/api/install/modpack",
      { method: "POST", body }
    );
    followJob(r.job_id, (target ? "Re-importing " : "Installing ") + label, () => {
      if (target) {
        openInstance(target);
      } else {
        loadInstances();
        showView("instances");
      }
    });
  } catch (e) {
    toast(e.message, "err");
  }
}

// --- Real-Time Background Jobs & Activity ------------------------------

// A job's stream, log and result live in this registry, not in the modal.
// "Run in Background" closes the view, not the connection, so a backgrounded
// install can be re-attached from Activity with its whole log intact -- and a
// `cancelled` arriving while no modal is open is harmless.
const jobRegistry = new Map();

// Five phases, derived from the step strings the backend already sends. The
// bar alone cannot say that a 3-minute download is followed by four short
// steps; the strip can.
const JOB_PHASES = ["Resolve", "Download", "Unpack", "Register", "Tune"];
const PHASE_PATTERNS = [
  [/resolv|manifest|prepar|analys|analyz|read|plan/i, 0],
  [/download|fetch|mod \d+ of|server pack/i, 1],
  [/unpack|extract|upload|writ|copy|overrid/i, 2],
  [/regist|creat|crafty|server entry/i, 3],
  [/tune|optimi|heap|flag|java|eula|final/i, 4],
];

function phaseFor(step) {
  if (!step) return 0;
  let idx = 0;
  PHASE_PATTERNS.forEach(([re, i]) => { if (re.test(step)) idx = Math.max(idx, i); });
  return idx;
}

function jobEntry(jobId, title, onDone, auto = false) {
  let entry = jobRegistry.get(jobId);
  if (entry) {
    if (onDone) entry.onDone = onDone;
    return entry;
  }
  entry = {
    id: jobId,
    title: title || "Task",
    onDone: onDone || null,
    // Jobs whose result drives the next screen (preflight review, an AI report,
    // a deep scan) hand off immediately. Everything else resolves into a
    // summary the user dismisses -- see renderJobSummary.
    auto: auto,
    status: "pending",
    step: "Starting task…",
    percent: 0,
    error: "",
    result: null,
    started: Date.now(),
    log: [],            // [{message, level, count}]
    seen: new Map(),    // dedupe key -> log index
    stream: "",
    view: null,         // the modal currently showing this job, if any
    es: null,
  };
  jobRegistry.set(jobId, entry);
  connectJob(entry);
  return entry;
}

function connectJob(entry) {
  const es = new EventSource(`/api/jobs/${entry.id}/events`);
  entry.es = es;
  es.onmessage = (ev) => {
    let d;
    try { d = JSON.parse(ev.data); } catch { return; }

    if (d.step) entry.step = d.step;
    if (typeof d.percent === "number") entry.percent = d.percent;
    if (d.status) entry.status = d.status;

    if (d.event === "log" && d.message) pushJobLog(entry, d.message, d.level || "info");
    if (d.event === "stream" && d.message) {
      entry.stream += d.message;
      entry.streamTotal = d.total || entry.stream.length;
    }
    if (d.event === "snapshot") {
      (d.log || []).forEach((l) => pushJobLog(entry, l.message, l.level));
      if (d.stream) { entry.stream = d.stream; entry.streamTotal = d.stream.length; }
    }

    if (entry.view) entry.view.update(d);

    if (d.event === "end" || ["done", "error", "cancelled"].includes(d.status)) {
      es.close();
      entry.es = null;
      entry.finished = true;
      entry.result = d.result || entry.result;
      entry.error = d.error || entry.error;
      if (d.status === "done") toast(entry.title + " finished", "ok");
      else if (d.status === "error") toast(entry.title + " failed", "err");
      else if (d.status === "cancelled") toast(entry.title + " cancelled");

      if (d.status === "done" && entry.onDone && !entry.ranDone) {
        entry.ranDone = true;
        try { entry.onDone(d); } catch (err) { console.error(err); }
      }
      if (entry.auto && d.status === "done") {
        if (entry.view) entry.view.modal.close();
      } else if (entry.view) {
        entry.view.settle();
      }
      loadJobs();
      refreshJobBadge();
    }
  };
  es.onerror = () => { es.close(); entry.es = null; };
}

// De-duplicated, but the repeat count is kept and shown rather than thrown
// away, so "downloading…" ×184 does not read as one download.
function pushJobLog(entry, message, level = "info") {
  const key = level + "|" + message;
  const at = entry.seen.get(key);
  if (at != null) {
    entry.log[at].count += 1;
    return;
  }
  entry.seen.set(key, entry.log.length);
  entry.log.push({ message, level, count: 1 });
}

const logLineHTML = (l) => `
  <div class="log-line ${l.level === "warn" ? "warn" : l.level === "error" ? "err" : ""}">
    <span class="lvl">${esc(l.level)}</span>
    <span class="msg">${esc(l.message)}</span>
    ${l.count > 1 ? `<span class="dupe">×${l.count}</span>` : ""}
  </div>`;

// Opens (or re-opens) a view onto a job. Everything already buffered is
// replayed, so re-attaching to a backgrounded install does not land the user
// on an empty console.
function openJobModal(entry) {
  if (entry.view) return entry.view.modal;

  let tick = null;
  const m = modal({
    title: entry.title,
    body: `
      <div class="job-head">
        <div style="flex:1;min-width:200px">
          <span class="section-label" id="jPhaseLabel">Phase 1 of 5</span>
          <div class="step" id="jStep">${esc(entry.step)}</div>
        </div>
        <div class="clock" id="jClock">0s elapsed</div>
      </div>
      <div class="bar" id="jBarWrap" role="progressbar" aria-label="Task progress"
           aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div id="jBar"></div></div>
      <div class="job-phases">
        ${JOB_PHASES.map((n) => `<div class="job-phase">${n}</div>`).join("")}
      </div>

      <div class="stream-wrap hidden" id="jStreamWrap">
        <div class="stream-head">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4l3 3h10l3-3-1 7 2 3-4 2-5 5-5-5-4-2 2-3z" fill="currentColor"></path></svg>
          Assistant output
          <span class="meta" id="jStreamMeta"></span>
        </div>
        <div class="log stream" id="jStream"></div>
      </div>

      <div class="row tight" style="margin:14px 0 7px">
        <span class="section-label" style="margin:0">Log</span>
        <span class="spacer" style="height:1px;background:var(--line)"></span>
        <span class="pill warn hidden" id="jWarnPill"></span>
      </div>
      <div class="log-console" id="jLog"></div>`,
    // Dismissing the view -- by button, X, Escape or backdrop -- detaches it.
    // The stream keeps running; only the view goes away.
    onClose: () => { entry.view = null; if (tick) clearInterval(tick); },
    actions: [
      // The backend has always supported cancelling a running job; nothing in
      // the UI ever called it, so a wrong 8-minute install had to be waited out.
      { label: "Cancel Task", cls: "danger", onClick: async (mm, btn) => {
          const ok = await confirmDialog({
            title: "Cancel task",
            message: `Stop "${entry.title}"? Work already written to the instance is left in place.`,
            confirmLabel: "Cancel task",
            danger: true,
          });
          if (!ok) return;
          btn.disabled = true;
          try {
            const res = await api(`/api/jobs/${entry.id}/cancel`, { method: "POST" });
            if (res.cancelled) { mm.error(""); toast("Cancellation requested", "ok"); }
            else mm.error("This task has already finished.");
          } catch (e) {
            mm.error(e.message);
            btn.disabled = false;
          }
        } },
      { label: "Run in Background", onClick: (mm) => mm.close() },
    ],
  });

  const el = m.el;
  const stepEl = $("#jStep", el), barEl = $("#jBar", el), barWrap = $("#jBarWrap", el);
  const phaseLabel = $("#jPhaseLabel", el), clockEl = $("#jClock", el);
  const logEl = $("#jLog", el), warnPill = $("#jWarnPill", el);
  const streamWrap = $("#jStreamWrap", el), streamEl = $("#jStream", el);
  const streamMeta = $("#jStreamMeta", el);
  const phaseEls = $$(".job-phase", el);
  const cancelBtn = m.buttons[0];

  tick = setInterval(() => {
    const secs = Math.round((Date.now() - entry.started) / 1000);
    clockEl.textContent = secs >= 60
      ? `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")} elapsed`
      : `${secs}s elapsed`;
  }, 500);

  const paintLog = () => {
    logEl.innerHTML = entry.log.map(logLineHTML).join("");
    logEl.scrollTop = logEl.scrollHeight;
    const warns = entry.log.filter((l) => l.level === "warn" || l.level === "error").length;
    warnPill.classList.toggle("hidden", !warns);
    warnPill.textContent = `${warns} warning${warns === 1 ? "" : "s"}`;
  };

  const paintStream = () => {
    if (!entry.stream) return;
    streamWrap.classList.remove("hidden");
    const pinned = streamEl.scrollHeight - streamEl.scrollTop - streamEl.clientHeight < 40;
    streamEl.textContent = entry.stream;
    streamMeta.textContent = `${(entry.streamTotal || entry.stream.length).toLocaleString()} chars`;
    if (pinned) streamEl.scrollTop = streamEl.scrollHeight;
  };

  const paintProgress = () => {
    stepEl.textContent = entry.step;
    const phase = phaseFor(entry.step);
    phaseLabel.textContent = `Phase ${phase + 1} of ${JOB_PHASES.length}`;
    phaseEls.forEach((p, i) => {
      p.classList.toggle("done", i < phase);
      p.classList.toggle("current", i === phase);
    });
    if (entry.percent) {
      barWrap.classList.remove("indeterminate");
      barEl.style.width = entry.percent + "%";
      barWrap.setAttribute("aria-valuenow", String(Math.round(entry.percent)));
    } else {
      barWrap.classList.add("indeterminate");
    }
  };

  const view = {
    modal: m,
    update: () => { paintProgress(); paintLog(); paintStream(); },
    settle: () => {
      if (tick) clearInterval(tick);
      if (cancelBtn) cancelBtn.disabled = true;
      renderJobSummary(entry, m);
    },
  };
  entry.view = view;
  view.update();
  if (entry.finished) view.settle();
  return m;
}

// A finished install used to close its own modal, destroying the log and every
// warning in it -- including the two that predict the server failing to boot.
// It now resolves into a summary the user dismisses themselves.
function renderJobSummary(entry, m) {
  const r = entry.result || {};
  const notes = entry.log.filter((l) => l.level === "warn" || l.level === "error");
  const problems = r.problems || [];
  const failed = entry.status === "error" || entry.status === "cancelled";

  const hero = failed
    ? `<div class="summary-hero err">
         <span class="glyph"><svg viewBox="0 0 24 24" fill="none" stroke="var(--red)" stroke-width="2.4" aria-hidden="true"><path d="M12 7v6M12 16.5v.5"></path><circle cx="12" cy="12" r="9"></circle></svg></span>
         <div><div class="t">${entry.status === "cancelled" ? "Cancelled" : "Failed"}</div>
           <div class="d">${esc(entry.error || (entry.status === "cancelled"
             ? "Stopped on request. Anything already written to the instance is left in place."
             : "The task did not finish."))}</div></div>
       </div>`
    : notes.length
      ? `<div class="summary-hero warn">
           <span class="glyph"><svg viewBox="0 0 24 24" fill="none" stroke="var(--amber)" stroke-width="2.2" aria-hidden="true"><path d="M12 4l9 16H3z"></path><path d="M12 10v4"></path></svg></span>
           <div><div class="t">Finished with ${notes.length} warning${notes.length === 1 ? "" : "s"}</div>
             <div class="d">Worth reading before you hit Start — some of these predict a failed first boot.</div></div>
         </div>`
      : `<div class="summary-hero">
           <span class="glyph"><svg viewBox="0 0 24 24" fill="none" stroke="var(--green)" stroke-width="2.6" aria-hidden="true"><path d="M5 13l4 4L19 7"></path></svg></span>
           <div><div class="t">Finished cleanly</div>
             <div class="d">${esc(r.pack ? `${r.pack} is registered in Crafty and ready to start.` : "Nothing needed attention.")}</div></div>
         </div>`;

  const stats = (r.mods_installed != null || r.files_uploaded != null) ? `
    <div class="statgrid" style="margin-top:14px">
      ${r.mods_installed != null ? `<div class="stat"><div class="k">Mods installed</div><div class="v">${esc(String(r.mods_installed))}</div></div>` : ""}
      ${r.files_uploaded != null ? `<div class="stat"><div class="k">Files uploaded</div><div class="v">${esc(String(r.files_uploaded))}</div></div>` : ""}
      ${problems.length ? `<div class="stat"><div class="k">Failed</div><div class="v warn">${problems.length}</div></div>` : ""}
      ${r.loader ? `<div class="stat"><div class="k">Runtime</div><div class="v">${esc(r.loader)}</div><div class="sub">${esc(r.minecraft || "")}</div></div>` : ""}
    </div>` : "";

  const noteList = notes.length ? `
    <span class="section-label" style="margin-top:16px">Warnings worth keeping</span>
    <div class="summary-list">
      ${notes.map((l) => `
        <div class="summary-note ${l.level === "error" ? "err" : "warn"}">
          <span class="glyph">!</span><span>${esc(l.message)}</span>
        </div>`).join("")}
    </div>` : "";

  // result.problems[] was carried by the API from the start and rendered
  // nowhere; its only trace was a count on the Modpack tab.
  const failList = problems.length ? `
    <span class="section-label" style="margin-top:16px">Could not be downloaded (${problems.length})</span>
    ${problems.map((pb) => `
      <div class="summary-fail">
        ${iconHTML(null, pb.name)}
        <div class="body">
          <div style="font-weight:550">${esc(pb.name || "unknown mod")}</div>
          <div class="reason">${esc(pb.reason || "")}</div>
        </div>
        <button class="btn btn-sm btn-secondary" data-add="${esc(pb.name || "")}">Add manually</button>
      </div>`).join("")}` : "";

  m.content.innerHTML = hero + stats + noteList + failList + `
    <details style="margin-top:16px">
      <summary class="faint">Full log (${entry.log.length} lines)</summary>
      <div class="log-console" style="margin-top:8px">${entry.log.map(logLineHTML).join("")}</div>
    </details>`;

  m.foot.innerHTML = "";
  m.buttons.length = 0;
  const addBtn = (label, cls, fn) => {
    const b = document.createElement("button");
    b.className = "btn btn-" + cls;
    b.textContent = label;
    b.onclick = fn;
    m.foot.appendChild(b);
    m.buttons.push(b);
    return b;
  };
  addBtn("Done", "primary", () => m.close());

  $$("[data-add]", m.content).forEach((b) => {
    b.onclick = () => { m.close(); openAddMod(b.dataset.add); };
  });
}

// Kept as the entry point every long operation already calls.
function followJob(jobId, title, onDone, { auto = false } = {}) {
  const entry = jobEntry(jobId, title, onDone, auto);
  return openJobModal(entry);
}

const JOB_ACTIVE = (status) => !["done", "error", "cancelled"].includes(status);

let jobsTimer = null;

// The list is a snapshot, so without this a cancelled job keeps claiming it is
// running until the user navigates away and back.
function scheduleJobsRefresh(hasActive) {
  clearTimeout(jobsTimer);
  if (!hasActive) return;
  jobsTimer = setTimeout(() => {
    if ($("#view-jobs").classList.contains("active")) loadJobs();
  }, 2500);
}

// Keep the nav badge honest even while the user is on another view.
async function refreshJobBadge() {
  try {
    const r = await api("/api/jobs");
    const active = (r.items || []).filter((j) => JOB_ACTIVE(j.status));
    $("#activeJobsBadge").classList.toggle("hidden", active.length === 0);
  } catch { /* the health pill already reports an unreachable backend */ }
}

async function loadJobs() {
  try {
    const r = await api("/api/jobs");
    const activeJobs = (r.items || []).filter((j) => JOB_ACTIVE(j.status));
    const badge = $("#activeJobsBadge");
    if (badge) {
      badge.classList.toggle("hidden", activeJobs.length === 0);
    }

    const statusPill = (st) => st === "done" ? "ok" : st === "error" ? "err"
                            : st === "cancelled" ? "" : "ok";

    $("#jobList").innerHTML = r.items.length ? r.items.map((j) => {
      const active = JOB_ACTIVE(j.status);
      const cls = j.status === "error" ? "error" : j.status === "cancelled" ? "cancelled"
                : j.status === "done" ? "done" : "running";
      return `
      <article class="job-card ${cls}">
        <div class="head">
          <span class="title">${esc(j.title)}</span>
          <span class="pill ${statusPill(j.status)}"><span class="pill-dot"></span>${esc(j.status)}</span>
          <div class="actions">
            ${active ? `<button class="btn btn-sm btn-secondary" data-watch="${esc(j.id)}">Watch</button>` : ""}
            ${active ? `<button class="btn btn-sm btn-danger" data-cancel="${esc(j.id)}">Cancel</button>` : ""}
          </div>
        </div>
        <div class="stepline">
          <span class="s">${esc(j.step || "")}</span>
          <span class="p">${j.percent ? j.percent + "%" : "—"}</span>
        </div>
        <div class="bar ${j.percent ? "" : active ? "indeterminate" : ""} ${j.status === "error" ? "err" : j.status === "cancelled" ? "cancelled" : ""}">
          <div style="width:${j.percent || 0}%"></div>
        </div>
        ${j.error ? `<div class="err-line">${esc(j.error)}</div>` : ""}
      </article>`;
    }).join("") : `
      <div class="empty">
        <div class="t">Nothing running</div>
        <div class="d">Installs, scans and AI analyses appear here while they work, and stay
          for an hour after they finish.</div>
      </div>`;

    // Run in Background used to be a one-way door: the job kept going but its
    // live stream was unreachable. Watch re-attaches a modal to the registry
    // entry and replays everything buffered so far.
    $$("[data-watch]", $("#jobList")).forEach((b) => {
      b.onclick = () => {
        const job = r.items.find((j) => j.id === b.dataset.watch);
        openJobModal(jobEntry(b.dataset.watch, job ? job.title : "Task"));
      };
    });

    $$("[data-cancel]", $("#jobList")).forEach((b) => {
      b.onclick = async () => {
        const job = r.items.find((j) => j.id === b.dataset.cancel);
        const ok = await confirmDialog({
          title: "Cancel task",
          message: `Stop "${job ? job.title : "this task"}"? Work already written to the instance is left in place.`,
          confirmLabel: "Cancel task",
          danger: true,
        });
        if (!ok) return;
        b.disabled = true;
        try {
          const res = await api(`/api/jobs/${b.dataset.cancel}/cancel`, { method: "POST" });
          toast(res.cancelled ? "Cancellation requested" : "Task had already finished",
                res.cancelled ? "ok" : "");
        } catch (e) {
          toast(e.message, "err");
          b.disabled = false;
        }
        loadJobs();
      };
    });

    scheduleJobsRefresh(activeJobs.length > 0);
  } catch (e) {
    toast(e.message, "err");
  }
}

// --- Initialization ---------------------------------------------------

// Dark stays the default; index.html has already applied the stored choice
// inline before first paint, so this only has to keep the two in sync.
(function themeToggle() {
  const btn = $("#themeToggle");
  if (!btn) return;
  const isLight = () => document.documentElement.getAttribute("data-theme") === "light";
  const sync = () => {
    btn.setAttribute("aria-pressed", String(isLight()));
    btn.setAttribute("aria-label", isLight() ? "Switch to dark theme" : "Switch to light theme");
  };
  btn.onclick = () => {
    const light = isLight();
    document.documentElement.setAttribute("data-theme", light ? "dark" : "light");
    try { localStorage.setItem("bf-theme", light ? "dark" : "light"); } catch (e) { /* private mode */ }
    sync();
  };
  sync();
})();

// The mod list binds once, on the container: virtualised rows mount and
// unmount as the user scrolls and must never need rebinding.
bindModList();

$("#importPackBtn").onclick = () => openImportModal();
$("#importCtaBtn").onclick = () => openImportModal();
$("#gotoImport").onclick = () => openImportModal();
$("#searchBtn").onclick = () => searchPacks(true);
$("#q").addEventListener("keydown", (e) => { if (e.key === "Enter") searchPacks(true); });
$("#morePacks").onclick = () => searchPacks(false);
$("#refreshInstances").onclick = loadInstances;

(async function boot() {
  await checkHealth();
  try {
    const v = await api("/api/meta/minecraft-versions");
    $("#mcv").innerHTML = `<option value="">All MC Versions</option>` +
      v.items.slice(0, 60).map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
  } catch { /* CurseForge might not be configured on initial run */ }
  await loadInstances();
  await refreshJobBadge();
  setInterval(checkHealth, 45000);
  setInterval(refreshJobBadge, 15000);
})();

})();
