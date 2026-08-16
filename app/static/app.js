/* BlessForge -- Single-Page Application (SPA) Frontend.
 * Modern Glassmorphic UI with full API, SSE background job streaming,
 * modpack installer, mod manager, config editor, diagnostics, AI assistant, and optimizer.
 */
(() => {
"use strict";

// --- Helpers -----------------------------------------------------------

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
  
  let icon = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>`;
  if (kind === "ok") {
    icon = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>`;
  } else if (kind === "err") {
    icon = `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg>`;
  }

  el.innerHTML = `
    <div style="display:flex;align-items:flex-start;gap:10px">
      <span style="color:inherit;flex-shrink:0;margin-top:1px">${icon}</span>
      <div style="flex:1;min-width:0;word-break:break-word">${esc(msg)}</div>
    </div>
  `;
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

function modal({ title, body, actions = [], wide = false }) {
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `
    <div class="modal" ${wide ? 'style="width:min(1100px,96vw)"' : ""}>
      <header>
        <div class="row tight">
          <strong style="font-size:16px">${esc(title)}</strong>
        </div>
        <button class="btn btn-sm btn-ghost" data-x title="Close modal">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
        </button>
      </header>
      <div class="content"></div>
      <div class="foot"></div>
    </div>`;
  const content = $(".content", back);
  if (typeof body === "string") content.innerHTML = body; else content.appendChild(body);
  const foot = $(".foot", back);
  const api2 = { el: back, content, foot, close: () => back.remove() };
  
  actions.forEach((a) => {
    const b = document.createElement("button");
    b.className = "btn " + (a.cls ? "btn-" + a.cls : "btn-secondary");
    b.textContent = a.label;
    b.onclick = () => a.onClick(api2, b);
    foot.appendChild(b);
  });
  
  $("[data-x]", back).onclick = api2.close;
  back.addEventListener("click", (e) => { if (e.target === back) api2.close(); });
  $("#modalRoot").appendChild(back);
  return api2;
}

function iconHTML(logo, name, cls = "mod-icon") {
  if (logo) return `<img class="${cls}" loading="lazy" src="${esc(logo)}" alt="">`;
  const letter = (String(name || "?").trim()[0] || "?").toUpperCase();
  return `<div class="${cls} placeholder">${esc(letter)}</div>`;
}

// --- State -------------------------------------------------------------

const state = {
  instances: [],
  inst: null,        // full detail of the open instance
  mods: [],
  configs: [],
  cfgPath: null,
  packIndex: 0,
  ai: { available: false },
};

// --- Top-Level Navigation ---------------------------------------------

$$("#topNav button").forEach((b) => { b.onclick = () => showView(b.dataset.view); });
$("#homeBtn").onclick = () => showView("instances");
$("#backToList").onclick = () => showView("instances");
$("#gotoBrowse").onclick = () => showView("browse");

function showView(name) {
  $$("#topNav button").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + name));
  if (name === "instances") loadInstances();
  if (name === "jobs") loadJobs();
  if (name === "browse" && !$("#packs").children.length) searchPacks(true);
  window.scrollTo(0, 0);
}

$$("#instTabs button").forEach((b) => { b.onclick = () => showTab(b.dataset.tab); });

function showTab(name) {
  $$("#instTabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name));
  $$(".tabpane").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "mods") loadMods();
  if (name === "configs") loadConfigs();
  if (name === "optimize") loadOptimize();
  if (name === "pack") loadPackTab();
  if (name === "troubleshoot") prewarmAI();
}

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
    el.innerHTML = `
      <span class="pill-dot"></span>
      <span class="pill-text">${h.ready ? `Crafty · ${c.crafty.servers} Servers` : "Crafty Unreachable"}</span>
    `;
    const problems = [];
    if (!c.crafty.ok) problems.push(`<strong>Crafty:</strong> ${esc(c.crafty.error)}`);
    if (!c.curseforge.ok) problems.push(`<strong>CurseForge:</strong> ${esc(c.curseforge.error)}`);
    const banner = $("#setup");
    if (problems.length) {
      banner.classList.remove("hidden");
      banner.className = "banner" + (!c.crafty.ok ? " err" : "");
      banner.innerHTML = `
        <div style="font-weight:700;margin-bottom:4px">⚠️ Setup Configuration Needed</div>
        <div>${problems.join(" &nbsp;·&nbsp; ")}</div>
        <div class="faint" style="margin-top:6px">Set <code>CRAFTY_URL</code>, <code>CRAFTY_TOKEN</code> and <code>CURSEFORGE_API_KEY</code> in environment variables or .env, then restart the container.</div>
      `;
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
    p.className = "pill pill-ai " + (ai.available ? "info" : "");
    p.innerHTML = `
      <span class="pill-dot"></span>
      <span class="pill-text">${ai.available ? `AI · ${ai.model.split(":")[0]}` : "AI Off"}</span>
    `;
    p.title = ai.available ? `Local model: ${ai.model}` : (ai.reason || "unavailable");
  } catch { /* AI is optional */ }
}

// --- Instances List ----------------------------------------------------

async function loadInstances() {
  const host = $("#instances");
  if (!host.children.length) {
    host.innerHTML = `<div class="card" style="grid-column:1/-1;text-align:center"><span class="spin"></span> Loading server instances…</div>`;
  }
  try {
    const r = await api("/api/instances");
    state.instances = r.items;
    
    if (!r.items.length) {
      host.innerHTML = `
        <div class="card empty" style="grid-column:1/-1">
          <svg viewBox="0 0 24 24" width="48" height="48" fill="none" stroke="var(--primary)" stroke-width="1.5" style="margin:0 auto 12px;display:block">
            <rect x="2" y="2" width="20" height="8" rx="2" ry="2"></rect>
            <rect x="2" y="14" width="20" height="8" rx="2" ry="2"></rect>
            <line x1="6" y1="6" x2="6.01" y2="6"></line>
            <line x1="6" y1="18" x2="6.01" y2="18"></line>
          </svg>
          <div style="font-size:16px;font-weight:700;color:var(--text-main);margin-bottom:6px">No Server Instances Found</div>
          <div class="faint" style="margin-bottom:16px">Install your first modpack from CurseForge to start managing servers.</div>
          <button class="btn btn-primary" onclick="showView('browse')">Browse Modpacks</button>
        </div>`;
      return;
    }

    host.innerHTML = r.items.map((i) => {
      const isRunning = Boolean(i.running);
      const memStat = i.mem ? `${i.mem}% RAM` : null;
      const cpuStat = i.cpu ? `${i.cpu}% CPU` : null;
      const playerStat = i.players != null ? `${i.players} online` : null;

      return `
        <div class="inst-card ${isRunning ? "running" : ""}" data-open="${esc(i.server_id)}">
          <div class="inst-card-head">
            <div style="flex:1;min-width:0">
              <div class="inst-card-title" title="${esc(i.name)}">${esc(i.name)}</div>
              <div class="inst-card-subtitle">
                ${i.pack ? `<strong>${esc(i.pack.name || "")}</strong> ${esc(i.pack.version || "")}` : "Custom Minecraft Server"}
              </div>
            </div>
            <span class="pill ${isRunning ? "ok" : ""}">
              <span class="pill-dot"></span>
              ${isRunning ? "Running" : "Stopped"}
            </span>
          </div>

          <div class="row tight" style="margin:8px 0">
            ${i.managed ? '<span class="pill accent">BlessForge</span>' : ""}
            ${i.loader ? `<span class="pill info">${esc(i.loader)}</span>` : ""}
            ${i.minecraft ? `<span class="pill">${esc(i.minecraft)}</span>` : ""}
            <span class="pill" title="Server Port">Port ${esc(i.port)}</span>
          </div>

          ${isRunning ? `
            <div class="inst-stats-bar">
              ${playerStat ? `<div class="inst-stat-item"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M23 21v-2a4 4 0 0 0-3-3.87"></path><path d="M16 3.13a4 4 0 0 1 0 7.75"></path></svg> ${playerStat}</div>` : ""}
              ${memStat ? `<div class="inst-stat-item"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="2" width="20" height="8" rx="2"></rect><rect x="2" y="14" width="20" height="8" rx="2"></rect></svg> ${memStat}</div>` : ""}
              ${cpuStat ? `<div class="inst-stat-item"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline></svg> ${cpuStat}</div>` : ""}
            </div>
          ` : ""}

          <div class="row" style="margin-top:auto;padding-top:14px;justify-content:space-between">
            <div class="row tight">
              <button class="btn btn-sm ${isRunning ? "btn-power-stop" : "btn-power-start"}" 
                      data-card-power="${isRunning ? "stop_server" : "start_server"}" 
                      data-sid="${esc(i.server_id)}"
                      onclick="event.stopPropagation(); executeCardPower(this)">
                ${isRunning ? "Stop" : "Start"}
              </button>
              ${isRunning ? `
                <button class="btn btn-sm btn-power-restart" 
                        data-card-power="restart_server" 
                        data-sid="${esc(i.server_id)}"
                        onclick="event.stopPropagation(); executeCardPower(this)">
                  Restart
                </button>
              ` : ""}
            </div>
            <button class="btn btn-sm btn-secondary" onclick="event.stopPropagation(); openInstance('${esc(i.server_id)}')">
              <span>Manage →</span>
            </button>
          </div>
        </div>`;
    }).join("");

    $$("#instances [data-open]").forEach((el) => {
      el.onclick = () => openInstance(el.dataset.open);
    });
  } catch (e) {
    toast(e.message, "err");
  }
}

window.executeCardPower = async (btn) => {
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
};

async function openInstance(id) {
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

function renderInstanceHead() {
  const i = state.inst;
  const running = Boolean(i.stats && i.stats.running);
  $("#crumbName").textContent = i.server.server_name;
  $("#instName").textContent = i.server.server_name;
  
  const statePill = $("#instState");
  statePill.className = "pill pill-status " + (running ? "ok" : "");
  statePill.innerHTML = `<span class="pill-dot"></span><span class="pill-text">${running ? "Running" : "Stopped"}</span>`;

  const bits = [];
  if (i.pack) {
    bits.push(`<span class="pill accent">${esc(i.pack.name || "")} ${esc(i.pack.version || "")}</span>`);
  }
  if (i.loader) {
    bits.push(`<span class="pill info">${esc(i.loader)}</span>`);
  }
  if (i.minecraft) {
    bits.push(`<span class="pill">${esc(i.minecraft)}</span>`);
  }
  bits.push(`<span class="pill">Port ${esc(i.server.server_port)}</span>`);
  if (i.stats && i.stats.online != null) {
    bits.push(`<span class="pill ok">${i.stats.online} Players Online</span>`);
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

async function loadMods() {
  const host = $("#modList");
  host.innerHTML = `<div class="card" style="text-align:center;margin:16px"><span class="spin"></span> Loading mods index…</div>`;
  try {
    const r = await api(`/api/instances/${state.inst.id}/mods`);
    state.mods = r.mods || [];
    $("#modStats").textContent = `${r.count} mods (${r.enabled} enabled · ${r.count - r.enabled} disabled)`;
    renderMods();

    // Fetch missing icons in background and repaint
    const missing = state.mods.some((m) => m.identified && !m.logo);
    if (missing) {
      api(`/api/instances/${state.inst.id}/mods/icons`, { method: "POST" })
        .then((res) => { if (res.updated) loadMods(); })
        .catch(() => {});
    }
  } catch (e) {
    host.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

function renderMods() {
  const filter = $("#modFilter").value.toLowerCase();
  const mode = $("#modState").value;
  const rows = state.mods.filter((m) => {
    if (mode === "enabled" && !m.enabled) return false;
    if (mode === "disabled" && m.enabled) return false;
    if (mode === "unidentified" && m.identified) return false;
    if (!filter) return true;
    return (m.name || "").toLowerCase().includes(filter) ||
           (m.file || "").toLowerCase().includes(filter);
  });

  $("#modList").innerHTML = rows.length ? rows.map((m) => `
    <div class="modrow ${m.enabled ? "" : "off"}" data-file="${esc(m.file)}">
      <label class="custom-checkbox">
        <input type="checkbox" class="modSel" data-file="${esc(m.file)}">
        <span class="checkbox-mark"></span>
      </label>
      ${iconHTML(m.logo, m.name)}
      <div class="mod-main">
        <div class="mod-name">
          <span>${esc(m.name || m.file)}</span>
          ${m.client_only_guess ? '<span class="pill warn" title="May be a client-only mod">client?</span>' : ""}
          ${m.required_by ? `<span class="pill info" title="Installed as dependency for ${esc(m.required_by)}">dep</span>` : ""}
        </div>
        <div class="mod-sub">${esc(m.file)}${m.size ? " · " + esc(m.size) : ""}</div>
      </div>
      <div class="mod-ver">
        <span class="cur mono" title="${esc(m.version || "unknown version")}">${esc(m.version || "—")}</span>
        ${m.project_id
          ? `<button class="btn btn-sm btn-secondary" data-ver="${esc(m.file)}">Change…</button>`
          : `<span class="faint" title="Run Identify Unknown to match project">n/a</span>`}
      </div>
      <div class="mod-actions">
        <label class="switch" title="${m.enabled ? "Disable" : "Enable"} mod">
          <input type="checkbox" class="modToggle" data-file="${esc(m.file)}" ${m.enabled ? "checked" : ""}>
          <span class="track"></span>
        </label>
        <button class="btn btn-sm btn-danger" data-del="${esc(m.file)}" title="Delete jar file">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
        </button>
      </div>
    </div>`).join("")
    : `<div class="empty">No mods match your filter criteria.</div>`;

  $$(".modToggle").forEach((t) => {
    t.onchange = async () => {
      t.disabled = true;
      try {
        await api(`/api/instances/${state.inst.id}/mods/toggle`, {
          method: "POST",
          body: { file: t.dataset.file, enabled: t.checked },
        });
        loadMods();
      } catch (e) {
        toast(e.message, "err");
        t.checked = !t.checked;
        t.disabled = false;
      }
    };
  });

  $$("[data-del]").forEach((b) => {
    b.onclick = async () => {
      if (!confirm(`Permanently delete ${b.dataset.del}? This cannot be undone.`)) return;
      try {
        await api(`/api/instances/${state.inst.id}/mods/delete`,
          { method: "POST", body: { files: [b.dataset.del] } });
        toast("Mod deleted", "ok");
        loadMods();
      } catch (e) {
        toast(e.message, "err");
      }
    };
  });

  $$("[data-ver]").forEach((b) => {
    b.onclick = () => {
      const mod = state.mods.find((m) => m.file === b.dataset.ver);
      if (mod) openVersionPicker(mod);
    };
  });

  $$(".modSel").forEach((c) => (c.onchange = updateBulkBar));
  updateBulkBar();
}

const selectedFiles = () => $$(".modSel:checked").map((c) => c.dataset.file);

function updateBulkBar() {
  const n = selectedFiles().length;
  $("#bulkBar").classList.toggle("hidden", n === 0);
  $("#selCount").textContent = `${n} mod(s) selected`;
}

$("#selAll").onchange = (e) => {
  $$(".modSel").forEach((c) => (c.checked = e.target.checked));
  updateBulkBar();
};

$$("[data-bulk]").forEach((b) => {
  b.onclick = async () => {
    const files = selectedFiles();
    if (!files.length) return;
    const act = b.dataset.bulk;
    if (act === "delete" && !confirm(`Permanently delete ${files.length} selected mods?`)) return;
    b.disabled = true;
    try {
      if (act === "delete") {
        await api(`/api/instances/${state.inst.id}/mods/delete`,
          { method: "POST", body: { files } });
      } else {
        await api(`/api/instances/${state.inst.id}/mods/bulk-toggle`,
          { method: "POST", body: { files, enabled: act === "enable" } });
      }
      toast(`${act === "enable" ? "Enabled" : act === "disable" ? "Disabled" : "Deleted"} ${files.length} mods`, "ok");
      $("#selAll").checked = false;
      loadMods();
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
    title: `Version Switcher — ${mod.name || mod.file}`,
    wide: true,
    body: `
      <div class="row" style="margin-bottom:14px">
        ${iconHTML(mod.logo, mod.name)}
        <div>
          <div style="font-size:15px"><strong>${esc(mod.name || mod.file)}</strong></div>
          <div class="faint">Currently: <span class="pill accent">${esc(mod.version || "unknown")}</span> · Source: ${esc(mod.source || "CurseForge")}</div>
        </div>
      </div>
      <label class="custom-checkbox" style="margin-bottom:12px">
        <input type="checkbox" id="vOnlyCompat" checked>
        <span class="checkbox-mark"></span>
        <span class="checkbox-label">Filter to ${esc(i.loader || "current loader")} for Minecraft ${esc(i.minecraft || "")}</span>
      </label>
      <div id="vBody"><span class="spin"></span> Fetching releases…</div>`,
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
        body.innerHTML = `<div class="empty">No compatible releases found for this loader and version.</div>`;
        return;
      }
      body.innerHTML = `
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Release</th>
                <th>Minecraft</th>
                <th>Loader</th>
                <th>Channel</th>
                <th>Date</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              ${r.items.map((f) => {
                const current = String(f.file_id) === String(mod.file_id);
                return `
                  <tr ${current ? 'style="background:rgba(245,158,11,0.08)"' : ""}>
                    <td>
                      <strong>${esc(f.display_name || f.version_number)}</strong>
                      ${current ? '<span class="pill accent" style="margin-left:6px">Active</span>' : ""}
                    </td>
                    <td class="faint mono">${esc((f.game_versions || []).slice(0, 3).join(", "))}</td>
                    <td class="faint mono">${esc((f.loaders || []).join(", "))}</td>
                    <td>
                      <span class="pill ${f.release_type === "release" ? "ok" : f.release_type === "beta" ? "warn" : "err"}">
                        ${esc(f.release_type || "release")}
                      </span>
                    </td>
                    <td class="faint">${esc((f.date || "").slice(0, 10))}</td>
                    <td style="text-align:right">
                      ${current ? '<span class="faint">Installed</span>' :
                        `<button class="btn btn-sm btn-primary" data-sw="${esc(String(f.file_id))}">Switch</button>`}
                    </td>
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
            followJob(res.job_id, `Switching ${mod.name || mod.file}`, () => loadMods());
          } catch (e) {
            toast(e.message, "err");
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

function openAddMod(prefill = "") {
  const i = state.inst;
  const m = modal({
    title: "Add Mod to Server",
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
    host.innerHTML = `<div style="text-align:center;padding:24px"><span class="spin"></span> Searching online catalog…</div>`;
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
          ${r.items.map((p) => `
            <div class="pack" style="cursor:default">
              ${iconHTML(p.logo, p.name, "mod-icon")}
              <div class="body">
                <div class="title">${esc(p.name)}</div>
                <div class="summary">${esc(p.summary || "")}</div>
                <div class="meta">
                  <span class="pill">${num(p.downloads)} ↓</span>
                  ${p.server_side ? `<span class="pill ${p.server_side === "unsupported" ? "err" : "ok"}">Server: ${esc(p.server_side)}</span>` : ""}
                </div>
              </div>
              <div style="display:flex;align-items:center">
                <button class="btn btn-sm btn-primary" data-pick="${esc(String(p.id))}"
                        data-src="${esc(p.source)}" data-name="${esc(p.name)}">
                  Select
                </button>
              </div>
            </div>`).join("")}
        </div>` : `<div class="empty">No matching mods found.</div>`;

      $$("[data-pick]", m.el).forEach((b) => {
        b.onclick = () => pickModVersion(b.dataset.src, b.dataset.pick, b.dataset.name, m);
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

async function pickModVersion(source, projectId, name, parent) {
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
      b.onclick = () => previewDependencies(source, projectId, b.dataset.add, name, parent);
    });
  } catch (e) {
    host.innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
  }
}

async function previewDependencies(source, projectId, fileId, name, parent) {
  const host = $("#amResults", parent.el);
  host.innerHTML = `<div style="text-align:center;padding:24px"><span class="spin"></span> Resolving recursive dependency graph…</div>`;
  try {
    const plan = await api(`/api/instances/${state.inst.id}/mods/resolve`, {
      method: "POST",
      body: { source, project_id: projectId, file_id: fileId },
    });
    const dep = plan.dependencies || [];
    const rows = dep.map((d) => `
      <div class="reviewrow">
        <label class="custom-checkbox">
          <input type="checkbox" class="depSel" data-pid="${esc(String(d.project_id))}" checked>
          <span class="checkbox-mark"></span>
        </label>
        ${iconHTML(d.logo, d.name)}
        <div class="body">
          <div><strong>${esc(d.name)}</strong> <span class="faint mono">${esc(d.version || "")}</span></div>
          <div class="reasons">Required by ${esc(d.required_by || name)}${d.size ? " · " + mb(d.size) : ""}</div>
        </div>
      </div>`).join("");

    host.innerHTML = `
      <h3 style="margin-bottom:12px">Ready to Install ${esc(name)}</h3>
      ${plan.root ? `
        <div class="reviewrow" style="background:rgba(245,158,11,0.06);border-radius:var(--radius-sm)">
          ${iconHTML(plan.root.logo, plan.root.name)}
          <div class="body">
            <div><strong>${esc(plan.root.name)}</strong> <span class="faint mono">${esc(plan.root.version || "")}</span></div>
            <div class="reasons">Selected target mod</div>
          </div>
        </div>` : ""}
      
      ${dep.length ? `
        <h4 style="margin-top:16px;margin-bottom:4px">Required Dependencies (${dep.length})</h4>
        <div class="faint" style="margin-bottom:10px">These will be downloaded and installed automatically to prevent launch crashes.</div>
        ${rows}
      ` : `<div class="faint" style="margin-top:12px">✨ No additional dependencies required.</div>`}

      ${plan.already_satisfied && plan.already_satisfied.length ? `
        <div class="faint" style="margin-top:10px">✓ ${plan.already_satisfied.length} dependencies are already installed.</div>
      ` : ""}
      ${(plan.warnings || []).map((w) => `<div class="banner" style="margin-top:10px">${esc(w)}</div>`).join("")}

      <div class="row" style="margin-top:18px;justify-content:flex-end">
        <button class="btn btn-primary" id="depGo">
          Install ${dep.length ? dep.length + 1 : 1} Mod(s)
        </button>
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
            name,
          },
        });
        parent.close();
        followJob(res.job_id, `Installing ${name}`, () => loadMods());
      } catch (e) {
        toast(e.message, "err");
      }
    };
  } catch (e) {
    host.innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
  }
}

$("#identifyMods").onclick = async () => {
  try {
    const r = await api(`/api/instances/${state.inst.id}/mods/identify`, { method: "POST" });
    followJob(r.job_id, "Identifying Mods via Fingerprints", () => loadMods());
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
      title: `Mod Updates (${r.updates.length} available out of ${r.checked} scanned)`,
      wide: true,
      body: r.updates.length ? `
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Mod Name</th>
                <th>Installed</th>
                <th>Latest Build</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              ${r.updates.map((u, idx) => `
                <tr>
                  <td><strong>${esc(u.name || u.file)}</strong></td>
                  <td class="faint mono">${esc(u.current_version || "—")}</td>
                  <td class="mono" style="color:var(--emerald)">${esc(u.latest_version || "—")}</td>
                  <td style="text-align:right">
                    <button class="btn btn-sm btn-primary" data-up="${idx}">Update</button>
                  </td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>`
        : `<div class="empty">✨ All identified mods are fully up to date!</div>`,
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
          followJob(res.job_id, `Updating ${u.name}`, () => loadMods());
        } catch (e) {
          toast(e.message, "err");
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
  $("#cfgList").innerHTML = `<div class="item faint"><span class="spin"></span> Scanning configuration files…</div>`;
  try {
    const r = await api(`/api/instances/${state.inst.id}/configs`);
    state.configs = r.files || [];
    renderConfigList();
  } catch (e) {
    $("#cfgList").innerHTML = `<div class="item faint">${esc(e.message)}</div>`;
  }
}

function renderConfigList() {
  const f = $("#cfgFilter").value.toLowerCase();
  const files = state.configs.filter((c) => !f || c.path.toLowerCase().includes(f));
  const groups = {};
  files.forEach((c) => (groups[c.owner] = groups[c.owner] || []).push(c));
  
  $("#cfgList").innerHTML = Object.keys(groups).sort().map((owner) =>
    `<div class="group">${esc(owner)} (${groups[owner].length})</div>` +
    groups[owner].map((c) =>
      `<div class="item ${c.path === state.cfgPath ? "active" : ""}"
            data-path="${esc(c.path)}" title="${esc(c.path)}">${esc(c.name)}</div>`
    ).join("")).join("") || `<div class="item faint" style="padding:16px">No config files found.</div>`;

  $$("#cfgList .item[data-path]").forEach((el) => {
    el.onclick = () => openConfig(el.dataset.path);
  });
}

async function openConfig(path) {
  state.cfgPath = path;
  renderConfigList();
  $("#cfgPath").textContent = path;
  $("#cfgEditor").value = "Loading file content…";
  $("#cfgSave").disabled = true;
  try {
    const r = await api(`/api/instances/${state.inst.id}/configs/read?path=${encodeURIComponent(path)}`);
    $("#cfgEditor").value = r.content;
    $("#cfgSave").disabled = !r.editable;
    $("#cfgStatus").textContent = `${r.lines} lines · ${r.language.toUpperCase()}`;
  } catch (e) {
    $("#cfgEditor").value = "";
    $("#cfgStatus").textContent = e.message;
  }
}

$("#cfgSave").onclick = async () => {
  const btn = $("#cfgSave");
  btn.disabled = true;
  try {
    await api(`/api/instances/${state.inst.id}/configs/write`, {
      method: "POST",
      body: { path: state.cfgPath, content: $("#cfgEditor").value },
    });
    toast(`Saved ${state.cfgPath}`, "ok");
    $("#cfgStatus").textContent = "✓ Saved";
  } catch (e) {
    toast(e.message, "err");
  } finally {
    btn.disabled = false;
  }
};
$("#cfgFilter").oninput = renderConfigList;

// --- Diagnostics & Troubleshooting Tab ---------------------------------

$("#runDiag").onclick = runDiagnostics;

$("#runDeep").onclick = async () => {
  try {
    const r = await api(`/api/instances/${state.inst.id}/deep-scan`, { method: "POST" });
    followJob(r.job_id, "Deep Dependency Graph Scan", (d) => {
      if (d.result) renderFindings(d.result.findings, `Deep Scan — ${d.result.scanned} Jars Analyzed`);
    });
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
    });
  } catch (e) {
    toast(e.message, "err");
  }
};

async function runDiagnostics() {
  const btn = $("#runDiag");
  btn.disabled = true;
  $("#diagOut").innerHTML = `<div class="card" style="text-align:center"><span class="spin"></span> Running diagnostic checks &amp; analyzing crash logs…</div>`;
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

function renderFindings(findings, title) {
  lastFindings = findings || [];
  if (!lastFindings.length) {
    $("#diagOut").innerHTML = `
      <div class="card" style="display:flex;align-items:center;gap:12px">
        <span class="pill ok"><span class="pill-dot"></span> All Clear</span>
        <span class="muted">${esc(title)} found no launch issues or broken dependencies.</span>
      </div>`;
    return;
  }
  $("#diagOut").innerHTML = `
    <div class="card">
      <h3 style="margin-bottom:14px">${esc(title)} — ${lastFindings.length} Finding(s)</h3>
      ${lastFindings.map((f, i) => `
        <div class="finding ${esc(f.severity || "info")}">
          <div class="row" style="align-items:flex-start">
            <div style="flex:1;min-width:0">
              <div class="t">${esc(f.title)}</div>
              <div class="d">${esc(f.detail)}</div>
            </div>
            ${f.fix ? `<button class="btn btn-sm btn-primary" data-fix="${i}">Fix Automatically</button>` : ""}
          </div>
          ${f.evidence ? `<pre>${esc(f.evidence)}</pre>` : ""}
        </div>`).join("")}
    </div>`;

  $$("[data-fix]").forEach((b) => {
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
        if (!confirm(`Disable ${fix.files.length} mod(s)?\n\n` + fix.files.join("\n"))) {
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
    title: "Incompatible Game Version Mods",
    wide: true,
    body: `<div id="fvBody"><span class="spin"></span> Searching compatible releases…</div>`,
  });
  try {
    const r = await api(`/api/instances/${state.inst.id}/fix/versions`, { method: "POST", body: { files } });
    const rows = (r.suggestions || []).map((s, i) => `
      <div class="reviewrow">
        <label class="custom-checkbox">
          <input type="checkbox" class="fvSel" data-i="${i}" checked>
          <span class="checkbox-mark"></span>
        </label>
        ${iconHTML(s.logo, s.name)}
        <div class="body">
          <div><strong>${esc(s.name || s.file)}</strong></div>
          <div class="reasons">${esc(s.current_version || s.file)} &nbsp;→&nbsp; <span class="pill ok">${esc(s.suggested_version)}</span></div>
        </div>
      </div>`).join("");

    $("#fvBody", m.el).innerHTML = `
      ${rows ? `
        <div class="faint" style="margin-bottom:10px">Compatible builds found for ${esc(r.loader || "")} ${esc(r.minecraft || "")}:</div>
        ${rows}
        <div class="row" style="justify-content:flex-end;margin-top:16px">
          <button class="btn btn-primary" id="fvGo">Switch Selected Mods</button>
        </div>`
        : `<div class="empty">No direct automated replacements found.</div>`}
      ${(r.unresolved || []).length ? `
        <h4 style="margin-top:18px">Manual Attention Required</h4>
        ${r.unresolved.map((u) => `<div class="faint">• ${esc(u.file)} — ${esc(u.why)}</div>`).join("")}
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
    title: "Install Missing Dependency: " + modId,
    wide: true,
    body: `<div id="depBody"><span class="spin"></span> Searching online sources…</div>`,
  });
  try {
    const r = await api(`/api/diagnose/dependency/${encodeURIComponent(modId)}?` + params);
    const section = (label, items) => (items && items.length)
      ? `<h4 style="margin-top:14px">${label}</h4><div class="pack-grid">` + items.map((p) => `
        <div class="pack" style="cursor:default">
          ${iconHTML(p.logo, p.name, "mod-icon")}
          <div class="body">
            <div class="title">${esc(p.name)}</div>
            <div class="summary">${esc(p.summary || "")}</div>
          </div>
          <button class="btn btn-sm btn-primary" data-dep="${esc(String(p.id))}"
                  data-src="${esc(p.source)}" data-name="${esc(p.name)}">Select</button>
        </div>`).join("") + "</div>" : "";

    $("#depBody", m.el).innerHTML =
      section("CurseForge", r.curseforge) + section("Modrinth", r.modrinth) ||
      `<div class="empty">No direct candidates found — it may be packaged inside another library.</div>`;

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

function renderAI(result) {
  const host = $("#aiOut");
  if (!result.available) {
    host.innerHTML = `
      <div class="banner">
        <strong>AI Assistant Unavailable:</strong> ${esc(result.reason || "")}
        ${result.hint ? `<div class="faint" style="margin-top:6px">${esc(result.hint)}</div>` : ""}
      </div>`;
    return;
  }
  if (!result.ok) {
    host.innerHTML = `
      <div class="banner err">
        <strong>AI Analysis Failed:</strong> ${esc(result.error || "")}
        ${result.partial ? `<pre class="log" style="margin-top:8px">${esc(result.partial)}</pre>` : ""}
      </div>`;
    return;
  }

  const confClass = { high: "ok", medium: "warn", low: "" }[result.confidence] || "";
  host.innerHTML = `
    <div class="ai-card">
      <div class="row" style="margin-bottom:10px">
        <div class="row tight">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="var(--cyan)" stroke-width="2"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"></path></svg>
          <strong style="font-size:16px">AI Diagnostic Report</strong>
        </div>
        <span class="pill ${confClass}">${esc(result.confidence)} confidence</span>
        <span class="pill info">${esc(result.model || "")}</span>
        <div class="spacer"></div>
        ${result.actions.length ? `<button class="btn btn-sm btn-primary" id="aiApplyAll">Apply Selected Fixes</button>` : ""}
      </div>
      
      <div style="font-size:14px;line-height:1.6;margin-bottom:12px">${esc(result.summary || "")}</div>
      ${result.notes ? `<div class="faint" style="margin-bottom:12px;background:rgba(0,0,0,0.2);padding:10px;border-radius:var(--radius-sm)">${esc(result.notes)}</div>` : ""}
      
      ${result.actions.length ? `
        <h4 style="margin:14px 0 6px">Recommended Actions (${result.actions.length})</h4>
        ${result.actions.map((a, i) => `
          <div class="ai-action">
            <label class="custom-checkbox">
              <input type="checkbox" class="aiSel" data-i="${i}" ${a.major ? "" : "checked"}>
              <span class="checkbox-mark"></span>
            </label>
            <div class="body" style="flex:1">
              <div class="row tight">
                <strong>${esc(a.description)}</strong>
                ${a.major ? '<span class="pill warn">Major Action</span>' : '<span class="pill ok">Safe</span>'}
              </div>
              <div class="faint" style="margin-top:2px">${esc(a.why)}</div>
              <div class="faint mono" style="font-size:11.5px;margin-top:4px;color:var(--cyan)">
                ${esc(a.action)}(${esc(JSON.stringify(a.args))})
              </div>
            </div>
          </div>`).join("")}
      ` : `<div class="faint" style="margin-top:10px">No automated actions suggested.</div>`}

      ${result.rejected && result.rejected.length ? `
        <div class="faint" style="margin-top:10px">Discarded ${result.rejected.length} invalid model suggestion(s).</div>
      ` : ""}
    </div>`;

  const applyBtn = $("#aiApplyAll");
  if (applyBtn) {
    applyBtn.onclick = async () => {
      const picked = $$(".aiSel:checked").map((c) => result.actions[Number(c.dataset.i)]);
      if (!picked.length) { toast("No actions selected"); return; }
      const major = picked.filter((a) => a.major);
      if (major.length) {
        const list = major.map((a) => `• ${a.description}`).join("\n");
        if (!confirm(`These actions modify or delete files:\n\n${list}\n\nExecute them now?`)) return;
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

function renderOptimize(p) {
  const h = p.host, m = p.memory;
  const pct = h.total_ram_gb ? Math.min(100, (m.heap_gb / h.total_ram_gb) * 100) : 0;
  const groups = {};
  p.flags.forEach((f, i) => (groups[f.group] = groups[f.group] || []).push({ ...f, i }));

  $("#optOut").innerHTML = `
    <div class="card">
      <h3 style="margin-bottom:14px">Host Hardware &amp; Resources</h3>
      <div class="statgrid">
        <div class="stat"><div class="k">Total System RAM</div><div class="v">${h.total_ram_gb} GB</div></div>
        <div class="stat"><div class="k">Available RAM</div><div class="v" style="color:var(--emerald)">${h.available_ram_gb} GB</div></div>
        <div class="stat"><div class="k">CPU Cores / Threads</div><div class="v">${h.cpu_count}</div></div>
        <div class="stat"><div class="k">Installed Mods</div><div class="v">${p.mod_count}</div></div>
      </div>
      ${h.cpu_model ? `<div class="faint mono" style="margin-top:6px">CPU: ${esc(h.cpu_model)}</div>` : ""}
      ${h.note ? `<div class="banner" style="margin-top:12px">${esc(h.note)}</div>` : ""}
    </div>

    <div class="card">
      <div class="row" style="margin-bottom:10px">
        <h3 style="margin:0">Heap Memory Allocation</h3>
        <div class="spacer"></div>
        <span class="pill info">Based on ${esc(m.basis)}</span>
      </div>

      <div class="row" style="margin-bottom:14px">
        <label style="display:flex;align-items:center;gap:10px">
          <span style="font-weight:600">Max Heap (GB):</span>
          <input type="number" id="optHeap" min="1" max="128" step="0.5" value="${m.heap_gb}" style="width:110px">
        </label>
        <div style="flex:1;min-width:200px">
          <div class="meter">
            <div style="width:${pct}%"></div>
            <span>${m.heap_gb} GB / ${h.total_ram_gb} GB System RAM</span>
          </div>
        </div>
      </div>

      <div class="faint">Pack recommended ${m.requested_gb} GB · Safe host ceiling is <strong>${m.ceiling_gb} GB</strong> (${m.reserve_gb} GB reserved for OS and Crafty).</div>
      ${m.warnings.map((w) => `<div class="banner" style="margin-top:12px">${esc(w)}</div>`).join("")}
      ${p.current.exists ? `
        <div class="faint mono" style="margin-top:10px;background:rgba(0,0,0,0.25);padding:8px 12px;border-radius:var(--radius-sm)">
          Currently: -Xms${p.current.xms_mb}M / -Xmx${p.current.xmx_mb}M with ${p.current.flags.length} active flags.
        </div>` : ""}
      ${p.note ? `<div class="banner" style="margin-top:12px">${esc(p.note)}</div>` : ""}
    </div>

    <div class="card">
      <div class="row" style="margin-bottom:8px">
        <h3 style="margin:0">Aikar's JVM Performance Flags</h3>
        <div class="spacer"></div>
        <div class="row tight">
          <button class="btn btn-sm btn-ghost" id="optAll">Select All</button>
          <button class="btn btn-sm btn-ghost" id="optNone">Select None</button>
          <button class="btn btn-sm btn-secondary" id="optRec">Reset to Recommended</button>
        </div>
      </div>
      <div class="faint" style="margin-bottom:12px">Independently toggleable Garbage Collection &amp; memory tuning flags.</div>
      
      ${Object.entries(groups).map(([group, items]) => `
        <div class="group" style="margin-top:12px;font-weight:700;color:var(--primary);text-transform:uppercase;font-size:12px">${esc(group)}</div>
        ${items.map((f) => `
          <div class="optrow">
            <label class="switch">
              <input type="checkbox" class="flagSel" data-flag="${esc(f.flag)}" data-rec="${f.recommended}" ${f.enabled ? "checked" : ""}>
              <span class="track"></span>
            </label>
            <div class="body" style="flex:1">
              <div class="row tight">
                <strong>${esc(f.label)}</strong>
                ${f.applied ? '<span class="pill ok">Active</span>' : ""}
              </div>
              <div class="flagname">${esc(f.flag)}</div>
              <div class="why">${esc(f.why)}</div>
            </div>
          </div>`).join("")}`).join("")}
    </div>

    <div class="card">
      <h3 style="margin-bottom:8px">Server Properties Optimization</h3>
      <div class="faint" style="margin-bottom:12px">Performance-tuned settings for tick rate stability.</div>
      ${p.properties.map((prop) => `
        <div class="optrow">
          <label class="switch">
            <input type="checkbox" class="propSel" data-key="${esc(prop.key)}" data-value="${esc(prop.value)}" ${prop.enabled ? "checked" : ""}>
            <span class="track"></span>
          </label>
          <div class="body" style="flex:1">
            <div class="row tight">
              <strong>${esc(prop.key)} = ${esc(prop.value)}</strong>
              ${prop.applied ? '<span class="pill ok">Active</span>' : prop.current != null ? `<span class="pill">Current: ${esc(prop.current)}</span>` : ""}
            </div>
            <div class="why">${esc(prop.why)}</div>
          </div>
        </div>`).join("")}
    </div>

    <div class="card row" style="background:var(--bg-card-elevated)">
      <div class="faint">Applied optimizations take effect on the next server start/restart.</div>
      <div class="spacer"></div>
      <button class="btn btn-primary" id="optApply">Apply Optimization Profile</button>
    </div>`;

  $("#optAll").onclick = () => $$(".flagSel").forEach((c) => (c.checked = true));
  $("#optNone").onclick = () => $$(".flagSel").forEach((c) => (c.checked = false));
  $("#optRec").onclick = () => $$(".flagSel").forEach((c) => (c.checked = c.dataset.rec === "true"));

  $("#optApply").onclick = async () => {
    const btn = $("#optApply");
    btn.disabled = true;
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
      (res.skipped || []).forEach((s) => toast(`${s.what}: ${s.why}`, "err"));
      loadOptimize();
    } catch (e) {
      toast(e.message, "err");
    } finally {
      btn.disabled = false;
    }
  };
}

// --- Modpack Tab -------------------------------------------------------

async function loadPackTab() {
  const i = state.inst;
  const pack = i.manifest.pack;
  const host = $("#packOut");
  if (!pack) {
    host.innerHTML = `
      <div class="card empty">
        This server instance was not deployed from a BlessForge modpack manifest, so there is no linked pack release. You can still manage all individual mods, configs, and optimization settings.
      </div>`;
    return;
  }
  host.innerHTML = `
    <div class="card">
      <h3 style="font-size:18px;margin-bottom:8px">${esc(pack.name || "")}</h3>
      <div class="row tight" style="margin-bottom:12px">
        <span class="pill accent">Installed Version ${esc(pack.version || "?")}</span>
        <span class="pill info">${esc(pack.install_source === "server_pack" ? "Official Server Pack" : "Assembled Manifest")}</span>
        <span class="pill">${esc(i.loader || "")} ${esc(i.minecraft || "")}</span>
      </div>
      ${(i.manifest.excluded_mods || []).length ? `<div class="faint" style="margin-top:8px">${i.manifest.excluded_mods.length} client-only mods were excluded during assembly.</div>` : ""}
      ${(i.manifest.problems || []).length ? `<div class="banner err" style="margin-top:10px">${i.manifest.problems.length} mods failed during download/unpack. Check Troubleshoot.</div>` : ""}
      <div class="row" style="margin-top:16px">
        <button class="btn btn-primary" id="switchPack">Switch Modpack Release Version</button>
      </div>
    </div>
    <div id="packVersions"></div>`;

  $("#switchPack").onclick = async () => {
    const box = $("#packVersions");
    box.innerHTML = `<div class="card" style="text-align:center"><span class="spin"></span> Loading modpack release history…</div>`;
    try {
      const r = await api(`/api/modpacks/${pack.project_id}/files?page_size=50`);
      box.innerHTML = `
        <div class="card">
          <h3 style="margin-bottom:8px">Select Target Modpack Release</h3>
          <div class="banner" style="margin-bottom:14px">Your world save is preserved. Mod jars and server configurations will be updated to match the selected release.</div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Release</th>
                  <th>MC Version</th>
                  <th>Source Type</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                ${r.items.map((f) => `
                  <tr>
                    <td>
                      <strong>${esc(f.display_name)}</strong>
                      ${String(f.file_id) === String(pack.file_id) ? '<span class="pill accent" style="margin-left:6px">Current</span>' : ""}
                    </td>
                    <td class="faint mono">${esc((f.game_versions || []).join(", "))}</td>
                    <td>
                      ${f.server_pack_file_id ? '<span class="pill ok">Server Pack</span>' : '<span class="pill warn">Manifest</span>'}
                    </td>
                    <td style="text-align:right">
                      ${String(f.file_id) === String(pack.file_id) ? "" :
                        `<button class="btn btn-sm btn-primary" data-pk="${esc(String(f.file_id))}">Switch</button>`}
                    </td>
                  </tr>`).join("")}
              </tbody>
            </table>
          </div>
        </div>`;

      $$("[data-pk]").forEach((b) => {
        b.onclick = async () => {
          if (!confirm("Update modpack version? World data will be preserved, but mods and configs will be updated.")) return;
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
      box.innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
    }
  };
}

// --- Browse & Install Modpacks -----------------------------------------

async function searchPacks(reset = true) {
  if (reset) {
    state.packIndex = 0;
    $("#packs").innerHTML = `<div class="card" style="grid-column:1/-1;text-align:center"><span class="spin"></span> Searching CurseForge catalog…</div>`;
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
    
    r.items.forEach((p) => {
      const el = document.createElement("div");
      el.className = "pack";
      const vers = [...new Set((p.latest_files || []).flatMap((f) => f.game_versions))].slice(0, 3);
      el.innerHTML = `
        ${iconHTML(p.logo, p.name, "mod-icon")}
        <div class="body">
          <div class="title">${esc(p.name)}</div>
          <div class="summary">${esc(p.summary || "")}</div>
          <div class="meta">
            <span class="pill">${num(p.downloads)} ↓</span>
            ${vers.map((v) => `<span class="pill info">${esc(v)}</span>`).join("")}
          </div>
        </div>`;
      el.onclick = () => openPack(p);
      host.appendChild(el);
    });

    state.packIndex += r.items.length;
    $("#morePacks").classList.toggle("hidden", r.items.length < 30);
    if (!r.items.length && reset) {
      $("#packs").innerHTML = `<div class="empty" style="grid-column:1/-1">No modpacks matched your search filters.</div>`;
    }
  } catch (e) {
    toast(e.message, "err");
  } finally {
    $("#searchBtn").disabled = false;
  }
}

async function openPack(pack) {
  const m = modal({
    title: pack.name,
    wide: true,
    body: `
      <div class="row" style="margin-bottom:16px;align-items:flex-start">
        ${iconHTML(pack.logo, pack.name, "mod-icon")}
        <div style="flex:1">
          <div style="font-size:14px;color:var(--text-main);margin-bottom:6px">${esc(pack.summary || "")}</div>
          <div class="row tight">
            <span class="pill">${num(pack.downloads)} Downloads</span>
            ${pack.url ? `<a href="${esc(pack.url)}" target="_blank" rel="noopener" class="btn btn-sm btn-ghost">View on CurseForge ↗</a>` : ""}
          </div>
        </div>
      </div>
      <h3 style="margin-bottom:10px">Available Releases</h3>
      <div id="verList"><span class="spin"></span> Loading releases…</div>`,
  });

  try {
    const r = await api(`/api/modpacks/${pack.id}/files?page_size=50`);
    $("#verList", m.el).innerHTML = `
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Release Name</th>
              <th>MC Version</th>
              <th>Loader</th>
              <th>Channel</th>
              <th>Source</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            ${r.items.map((f, idx) => `
              <tr>
                <td><strong>${esc(f.display_name)}</strong></td>
                <td class="faint mono">${esc((f.game_versions || []).join(", "))}</td>
                <td class="faint mono">${esc((f.loaders || []).join(", "))}</td>
                <td><span class="pill ${f.release_type === "release" ? "ok" : "warn"}">${esc(f.release_type)}</span></td>
                <td>
                  ${f.server_pack_file_id ? '<span class="pill ok">Server Pack</span>' : '<span class="pill warn">Manifest</span>'}
                </td>
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
    $("#verList", m.el).innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
  }
}

function installWizard(pack, file) {
  const m = modal({
    title: "Install Modpack — " + pack.name,
    body: `
      <div class="pill info" style="margin-bottom:16px">${esc(file.display_name)}</div>
      
      <div class="row" style="margin-bottom:14px">
        <label style="flex:1">Server Name<br>
          <input type="text" id="iName" value="${esc(pack.name.slice(0, 60))}" style="width:100%">
        </label>
        <label>Server Port<br>
          <input type="number" id="iPort" value="25565" style="width:110px">
        </label>
      </div>

      <div style="display:flex;flex-direction:column;gap:10px;margin:16px 0">
        <label class="custom-checkbox">
          <input type="checkbox" id="iServerPack" ${file.server_pack_file_id ? "checked" : "disabled"}>
          <span class="checkbox-mark"></span>
          <span class="checkbox-label">Prefer official server pack build ${file.server_pack_file_id ? "" : "(no official server build; assemble via manifest)"}</span>
        </label>

        <label class="custom-checkbox">
          <input type="checkbox" id="iReview" checked>
          <span class="checkbox-mark"></span>
          <span class="checkbox-label">Review and strip client-only mods before provisioning</span>
        </label>

        <label class="custom-checkbox">
          <input type="checkbox" id="iOptimize" checked>
          <span class="checkbox-mark"></span>
          <span class="checkbox-label">Automatically tune JVM flags &amp; memory allocations for this host</span>
        </label>
      </div>`,
    actions: [
      { label: "Cancel", onClick: (mm) => mm.close() },
      { label: "Continue", cls: "primary", onClick: async (mm, btn) => {
          btn.disabled = true;
          const opts = {
            mod_id: pack.id,
            file_id: file.file_id,
            server_name: $("#iName", mm.el).value.trim() || pack.name,
            port: Number($("#iPort", mm.el).value) || 25565,
            prefer_server_pack: $("#iServerPack", mm.el).checked,
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

async function startPreflight(pack, opts) {
  try {
    const r = await api("/api/install/preflight", {
      method: "POST",
      body: {
        mod_id: opts.mod_id,
        file_id: opts.file_id,
        prefer_server_pack: opts.prefer_server_pack,
      },
    });
    followJob(r.job_id, "Analyzing Pack " + pack.name, (d) => {
      if (d.result) showReview(pack, opts, d.result);
    });
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
    return `
      <div class="reviewrow ${keep ? "keep" : ""}">
        <label class="custom-checkbox">
          <input type="checkbox" class="rvSel" data-i="${i}" data-file="${esc(c.file_name)}" ${remove ? "checked" : ""}>
          <span class="checkbox-mark"></span>
        </label>
        ${iconHTML(c.logo, c.name)}
        <div class="body" style="flex:1">
          <div>
            <strong>${esc(c.name || c.file_name)}</strong>
            ${remove ? '<span class="pill err" style="margin-left:6px">Client-Only</span>' : ""}
            ${c.recommendation === "review" ? '<span class="pill warn" style="margin-left:6px">Review Needed</span>' : ""}
            ${keep ? '<span class="pill ok" style="margin-left:6px">Keep — Dependent Present</span>' : ""}
          </div>
          <div class="reasons">${esc((c.reasons || []).join(" · "))}</div>
          ${c.required_by_others ? `<div class="reasons" style="color:var(--emerald)">Required by ${esc(c.required_by_others.join(", "))}</div>` : ""}
          <div class="mod-sub">${esc(c.file_name)}</div>
        </div>
      </div>`;
  }).join("");

  modal({
    title: "Preflight Client Mod Review",
    wide: true,
    body: `
      <div class="statgrid">
        <div class="stat"><div class="k">Modpack</div><div class="v" style="font-size:15px">${esc(analysis.pack.name || pack.name)}</div></div>
        <div class="stat"><div class="k">Runtime</div><div class="v" style="font-size:15px">${esc(analysis.loader)} ${esc(analysis.minecraft)}</div></div>
        <div class="stat"><div class="k">Total Mods</div><div class="v">${review.total_mods ?? "—"}</div></div>
        <div class="stat"><div class="k">Tuned Heap</div><div class="v">${mem.heap_gb ?? "—"} GB</div></div>
      </div>
      
      ${(mem.warnings || []).map((w) => `<div class="banner">${esc(w)}</div>`).join("")}
      ${(analysis.warnings || []).map((w) => `<div class="banner">${esc(w)}</div>`).join("")}

      <h4 style="margin:16px 0 6px">Candidate Client-Only Mods (${items.length})</h4>
      <div class="faint" style="margin-bottom:12px">
        Checked mods will be excluded from the server install. Mods marked "Keep" are preserved because another server-side mod requires them.
      </div>
      ${items.length ? rows : `<div class="empty">✨ No client-only jars detected. Ready to install!</div>`}`,
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
  try {
    const r = await api("/api/install/modpack", { method: "POST", body: opts });
    followJob(r.job_id, "Installing " + label, () => {
      loadInstances();
      showView("instances");
    });
  } catch (e) {
    toast(e.message, "err");
  }
}

// --- Real-Time Background Jobs & Activity ------------------------------

function followJob(jobId, title, onDone) {
  const m = modal({
    title,
    body: `
      <div class="row" style="margin-bottom:6px">
        <div id="jStep" style="font-weight:600;color:var(--text-main)">Starting task…</div>
        <div class="spacer"></div>
        <span class="pill" id="jClock">0s</span>
      </div>
      <div class="bar"><div id="jBar"></div></div>
      
      <div class="hidden" id="jStreamWrap" style="margin-top:12px">
        <div class="row" style="margin-bottom:6px">
          <strong style="color:var(--cyan);font-size:13px">AI Stream Output</strong>
          <div class="spacer"></div>
          <span class="faint mono" id="jStreamMeta"></span>
        </div>
        <div class="log stream" id="jStream"></div>
      </div>

      <div class="log" id="jLog" style="margin-top:12px"></div>`,
    actions: [{ label: "Run in Background", onClick: (mm) => mm.close() }],
  });

  const stepEl = $("#jStep", m.el), barEl = $("#jBar", m.el);
  const logEl = $("#jLog", m.el), clockEl = $("#jClock", m.el);
  const streamWrap = $("#jStreamWrap", m.el), streamEl = $("#jStream", m.el);
  const streamMeta = $("#jStreamMeta", m.el);

  const started = Date.now();
  const ticker = setInterval(() => {
    clockEl.textContent = Math.round((Date.now() - started) / 1000) + "s";
  }, 500);
  const stop = () => { clearInterval(ticker); };

  const seen = new Set();
  const addLog = (msg, level = "info") => {
    const key = level + msg;
    if (seen.has(key)) return;
    seen.add(key);
    const d = document.createElement("div");
    d.className = level;
    d.textContent = msg;
    logEl.appendChild(d);
    logEl.scrollTop = logEl.scrollHeight;
  };

  const addStream = (text, total) => {
    if (!text) return;
    streamWrap.classList.remove("hidden");
    const pinned = streamEl.scrollHeight - streamEl.scrollTop - streamEl.clientHeight < 40;
    streamEl.textContent += text;
    if (total) streamMeta.textContent = `${total.toLocaleString()} chars`;
    if (pinned) streamEl.scrollTop = streamEl.scrollHeight;
  };

  const es = new EventSource(`/api/jobs/${jobId}/events`);
  es.onmessage = (ev) => {
    let d;
    try { d = JSON.parse(ev.data); } catch { return; }
    if (d.step) stepEl.textContent = d.step;
    if (typeof d.percent === "number") barEl.style.width = d.percent + "%";
    if (d.event === "log" && d.message) addLog(d.message, d.level || "info");
    if (d.event === "stream") addStream(d.message, d.total);
    if (d.event === "snapshot") {
      (d.log || []).forEach((l) => addLog(l.message, l.level));
      if (d.stream) { streamEl.textContent = ""; addStream(d.stream, d.stream.length); }
    }
    if (d.event === "end" || ["done", "error", "cancelled"].includes(d.status)) {
      es.close();
      stop();
      if (d.status === "done") {
        stepEl.innerHTML = `<span class="pill ok">Completed Successfully</span>`;
        barEl.style.width = "100%";
        toast(title + " finished", "ok");
        if (onDone) { m.close(); onDone(d); }
      } else if (d.status === "error") {
        stepEl.innerHTML = `<span class="pill err">Task Failed</span> ${esc(d.error || "")}`;
        toast(title + " encountered an error", "err");
      }
    }
  };
  es.onerror = () => { es.close(); stop(); };
  return m;
}

async function loadJobs() {
  try {
    const r = await api("/api/jobs");
    const activeJobs = (r.items || []).filter((j) => !["done", "error", "cancelled"].includes(j.status));
    const badge = $("#activeJobsBadge");
    if (badge) {
      badge.classList.toggle("hidden", activeJobs.length === 0);
    }

    $("#jobList").innerHTML = r.items.length ? r.items.map((j) => `
      <div class="card">
        <div class="row" style="margin-bottom:6px">
          <strong style="font-size:15px">${esc(j.title)}</strong>
          <span class="pill ${j.status === "done" ? "ok" : j.status === "error" ? "err" : "info"}">${esc(j.status)}</span>
          <div class="spacer"></div>
          <span class="faint mono">${esc(j.step || "")}${j.percent ? " · " + j.percent + "%" : ""}</span>
        </div>
        ${j.percent ? `<div class="bar" style="margin:6px 0"><div style="width:${j.percent}%"></div></div>` : ""}
        ${j.error ? `<div class="faint" style="color:var(--rose);margin-top:6px">Error: ${esc(j.error)}</div>` : ""}
      </div>`).join("") : `<div class="card empty">No recent tasks or background jobs.</div>`;
  } catch (e) {
    toast(e.message, "err");
  }
}

// --- Initialization ---------------------------------------------------

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
  setInterval(checkHealth, 45000);
})();

})();
