/* BlessForge -- single-page frontend, no build step.
 *
 * Navigation model: a flat list of instances, and everything else about an
 * instance (mods, configs, troubleshooting, tuning) lives inside that
 * instance's own page as tabs. Nothing operates on an implicit "current
 * server" picked from a dropdown somewhere else.
 */
(() => {
"use strict";

// --- helpers -----------------------------------------------------------

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
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "err" ? 9000 : 4200);
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
  back.innerHTML = `<div class="modal" ${wide ? 'style="width:min(1100px,100%)"' : ""}>
      <header class="row"><strong>${esc(title)}</strong><div class="spacer"></div>
        <button class="btn sm" data-x>Close</button></header>
      <div class="content"></div>
      <div class="foot"></div></div>`;
  const content = $(".content", back);
  if (typeof body === "string") content.innerHTML = body; else content.appendChild(body);
  const foot = $(".foot", back);
  const api2 = { el: back, content, foot, close: () => back.remove() };
  actions.forEach((a) => {
    const b = document.createElement("button");
    b.className = "btn " + (a.cls || "");
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

// --- state -------------------------------------------------------------

const state = {
  instances: [],
  inst: null,        // full detail of the open instance
  mods: [],
  configs: [],
  cfgPath: null,
  packIndex: 0,
  ai: { available: false },
};

// --- top-level navigation ---------------------------------------------

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

// Loading a 2 GB model off disk costs ~25s. Kick that off as soon as the
// Troubleshoot tab opens so it is already resident if the user asks.
let warmed = false;
function prewarmAI() {
  if (warmed || !state.ai.available) return;
  warmed = true;
  $("#aiHint").textContent = "warming up the local model…";
  api("/api/ai/warm", { method: "POST" })
    .then((r) => {
      $("#aiHint").textContent = r.warmed
        ? `${state.ai.model} ready` : "";
    })
    .catch(() => { $("#aiHint").textContent = ""; });
}

// --- health ------------------------------------------------------------

async function checkHealth() {
  try {
    const h = await api("/api/health");
    const el = $("#health");
    const c = h.checks;
    el.className = h.ready ? "pill ok" : "pill err";
    el.textContent = h.ready ? `Crafty · ${c.crafty.servers} servers`
                             : "Crafty unreachable";
    const problems = [];
    if (!c.crafty.ok) problems.push(`<b>Crafty:</b> ${esc(c.crafty.error)}`);
    if (!c.curseforge.ok) problems.push(`<b>CurseForge:</b> ${esc(c.curseforge.error)}`);
    const banner = $("#setup");
    if (problems.length) {
      banner.classList.remove("hidden");
      banner.className = "banner" + (!c.crafty.ok ? " err" : "");
      banner.innerHTML = "Setup needed — " + problems.join(" &nbsp;·&nbsp; ") +
        `<div class="faint" style="margin-top:6px">Set CRAFTY_URL, CRAFTY_TOKEN
         and CURSEFORGE_API_KEY on the container, then restart it.</div>`;
    } else banner.classList.add("hidden");
  } catch {
    $("#health").className = "pill err";
    $("#health").textContent = "backend down";
  }
  try {
    const ai = await api("/api/ai/status");
    state.ai = ai;
    const p = $("#aiPill");
    p.classList.remove("hidden");
    p.className = ai.available ? "pill info" : "pill";
    p.textContent = ai.available ? `AI · ${ai.model.split(":")[0]}` : "AI off";
    p.title = ai.available ? `Local model: ${ai.model}`
                           : (ai.reason || "unavailable");
  } catch { /* AI is optional */ }
}

// --- instances list ----------------------------------------------------

async function loadInstances() {
  const host = $("#instances");
  if (!host.children.length) host.innerHTML = `<div class="card"><span class="spin"></span> loading…</div>`;
  try {
    const r = await api("/api/instances");
    state.instances = r.items;
    host.innerHTML = r.items.length ? r.items.map((i) => `
      <div class="inst-card" data-open="${i.server_id}">
        <div class="row tight" style="margin-bottom:2px">
          <span class="title">${esc(i.name)}</span>
          <span class="pill ${i.running ? "ok" : ""}">${i.running ? "running" : "stopped"}</span>
          ${i.managed ? '<span class="pill accent">BlessForge</span>' : ""}
        </div>
        <div class="faint">
          ${i.pack ? esc(i.pack.name || "") + " · " : ""}
          ${esc(i.loader || "unknown loader")}${i.minecraft ? " " + esc(i.minecraft) : ""}
          · port ${esc(i.port)}
        </div>
      </div>`).join("")
      : `<div class="empty">No instances yet — install a modpack to create one.</div>`;
    $$("#instances [data-open]").forEach((el) => {
      el.onclick = () => openInstance(el.dataset.open);
    });
  } catch (e) { toast(e.message, "err"); }
}

async function openInstance(id) {
  showView("instance");
  $("#instName").textContent = "Loading…";
  $("#instMeta").textContent = "";
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
  } catch (e) { toast(e.message, "err"); }
}

function renderInstanceHead() {
  const i = state.inst;
  const running = i.stats && i.stats.running;
  $("#crumbName").textContent = i.server.server_name;
  $("#instName").textContent = i.server.server_name;
  $("#instState").className = "pill " + (running ? "ok" : "");
  $("#instState").textContent = running ? "running" : "stopped";
  const bits = [];
  if (i.pack) bits.push(`${esc(i.pack.name || "")} ${esc(i.pack.version || "")}`);
  if (i.loader) bits.push(esc(i.loader) + (i.minecraft ? " " + esc(i.minecraft) : ""));
  bits.push("port " + esc(i.server.server_port));
  $("#instMeta").innerHTML = bits.join(" &nbsp;·&nbsp; ");
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
      toast(b.dataset.power.replace("_", " ") + " sent", "ok");
      setTimeout(async () => {
        const d = await api(`/api/instances/${state.inst.id}`);
        state.inst.stats = d.stats; renderInstanceHead();
      }, 5000);
    } catch (e) { toast(e.message, "err"); }
    finally { b.disabled = false; }
  };
});

// --- mods --------------------------------------------------------------

async function loadMods() {
  const host = $("#modList");
  host.innerHTML = `<div class="card"><span class="spin"></span> loading mods…</div>`;
  try {
    const r = await api(`/api/instances/${state.inst.id}/mods`);
    state.mods = r.mods || [];
    $("#modStats").textContent =
      `${r.count} mods · ${r.enabled} on · ${r.count - r.enabled} off`;
    renderMods();
    // Fill in any missing icons in the background, then repaint once.
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

  $("#modList").innerHTML = rows.length ? rows.map((m, i) => `
    <div class="modrow ${m.enabled ? "" : "off"}" data-file="${esc(m.file)}">
      <input type="checkbox" class="modSel" data-file="${esc(m.file)}">
      ${iconHTML(m.logo, m.name)}
      <div class="mod-main">
        <div class="mod-name">${esc(m.name || m.file)}
          ${m.client_only_guess ? '<span class="pill warn" title="Looks client-only">client?</span>' : ""}
          ${m.required_by ? `<span class="pill" title="Installed as a dependency of ${esc(m.required_by)}">dep</span>` : ""}
        </div>
        <div class="mod-sub">${esc(m.file)}${m.size ? " · " + esc(m.size) : ""}</div>
      </div>
      <div class="mod-ver">
        <span class="cur" title="${esc(m.version || "unknown version")}">${esc(m.version || "—")}</span>
        ${m.project_id
          ? `<button class="btn sm" data-ver="${esc(m.file)}">Change…</button>`
          : `<span class="faint" title="Run Identify to enable version switching">n/a</span>`}
      </div>
      <div class="mod-actions">
        <label class="switch" title="${m.enabled ? "Disable" : "Enable"} this mod">
          <input type="checkbox" class="modToggle" data-file="${esc(m.file)}" ${m.enabled ? "checked" : ""}>
          <span class="track"></span>
        </label>
        <button class="btn sm danger" data-del="${esc(m.file)}">Del</button>
      </div>
    </div>`).join("")
    : `<div class="empty">No mods match.</div>`;

  $$(".modToggle").forEach((t) => {
    t.onchange = async () => {
      t.disabled = true;
      try {
        await api(`/api/instances/${state.inst.id}/mods/toggle`, {
          method: "POST",
          body: { file: t.dataset.file, enabled: t.checked },
        });
        loadMods();
      } catch (e) { toast(e.message, "err"); t.checked = !t.checked; t.disabled = false; }
    };
  });
  $$("[data-del]").forEach((b) => {
    b.onclick = async () => {
      if (!confirm(`Delete ${b.dataset.del}? This cannot be undone.`)) return;
      try {
        await api(`/api/instances/${state.inst.id}/mods/delete`,
          { method: "POST", body: { files: [b.dataset.del] } });
        toast("Deleted", "ok"); loadMods();
      } catch (e) { toast(e.message, "err"); }
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
  $("#selCount").textContent = `${n} selected`;
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
    if (act === "delete" &&
        !confirm(`Delete ${files.length} mods? This cannot be undone.`)) return;
    b.disabled = true;
    try {
      if (act === "delete") {
        await api(`/api/instances/${state.inst.id}/mods/delete`,
          { method: "POST", body: { files } });
      } else {
        await api(`/api/instances/${state.inst.id}/mods/bulk-toggle`,
          { method: "POST", body: { files, enabled: act === "enable" } });
      }
      toast(`${act}: ${files.length} mods`, "ok");
      $("#selAll").checked = false;
      loadMods();
    } catch (e) { toast(e.message, "err"); }
    finally { b.disabled = false; }
  };
});

$("#modFilter").oninput = renderMods;
$("#modState").onchange = renderMods;

// --- version switching -------------------------------------------------

async function openVersionPicker(mod) {
  const i = state.inst;
  const m = modal({
    title: `Versions — ${mod.name || mod.file}`,
    wide: true,
    body: `<div class="row" style="margin-bottom:12px">
        ${iconHTML(mod.logo, mod.name)}
        <div>
          <div><strong>${esc(mod.name || mod.file)}</strong></div>
          <div class="faint">Installed: <span class="pill accent">${esc(mod.version || "unknown")}</span>
            &nbsp;·&nbsp; ${esc(mod.source || "")}</div>
        </div>
      </div>
      <label class="check" style="margin-bottom:10px">
        <input type="checkbox" id="vOnlyCompat" checked>
        Only versions matching ${esc(i.loader || "this loader")} ${esc(i.minecraft || "")}
      </label>
      <div id="vBody"><span class="spin"></span> loading versions…</div>`,
  });

  const load = async () => {
    const body = $("#vBody", m.el);
    body.innerHTML = `<span class="spin"></span> loading versions…`;
    const params = new URLSearchParams();
    if ($("#vOnlyCompat", m.el).checked) {
      if (i.minecraft) params.set("game_version", i.minecraft);
      if (i.loader) params.set("loader", i.loader);
    }
    try {
      const r = await api(
        `/api/mods/${mod.source || "curseforge"}/${mod.project_id}/versions?` + params);
      if (!r.items.length) {
        body.innerHTML = `<div class="empty">No versions found with these filters.</div>`;
        return;
      }
      body.innerHTML = `<div class="table-wrap"><table>
        <thead><tr><th>Version</th><th>Minecraft</th><th>Loader</th>
          <th>Type</th><th>Released</th><th></th></tr></thead>
        <tbody>${r.items.map((f) => {
          const current = String(f.file_id) === String(mod.file_id);
          return `<tr${current ? ' style="background:var(--bg-3)"' : ""}>
            <td>${esc(f.display_name || f.version_number)}
              ${current ? '<span class="pill accent">installed</span>' : ""}</td>
            <td class="faint">${esc((f.game_versions || []).slice(0, 3).join(", "))}</td>
            <td class="faint">${esc((f.loaders || []).join(", "))}</td>
            <td><span class="pill ${f.release_type === "release" ? "ok" : "warn"}">${esc(f.release_type || "")}</span></td>
            <td class="faint">${esc((f.date || "").slice(0, 10))}</td>
            <td>${current ? "" :
              `<button class="btn sm primary" data-sw="${esc(String(f.file_id))}">Switch</button>`}</td>
          </tr>`;
        }).join("")}</tbody></table></div>`;

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
                      () => loadMods());
          } catch (e) { toast(e.message, "err"); b.disabled = false; }
        };
      });
    } catch (e) { body.innerHTML = `<div class="banner err">${esc(e.message)}</div>`; }
  };
  $("#vOnlyCompat", m.el).onchange = load;
  load();
}

// --- adding mods (with dependency preview) -----------------------------

$("#addModBtn").onclick = () => openAddMod();

function openAddMod(prefill = "") {
  const i = state.inst;
  const m = modal({
    title: "Add a mod",
    wide: true,
    body: `<div class="row" style="margin-bottom:10px">
        <input type="search" id="amQ" placeholder="Search mods…" value="${esc(prefill)}" style="flex:1">
        <select id="amSrc">
          <option value="curseforge">CurseForge</option>
          <option value="modrinth">Modrinth</option>
        </select>
        <button class="btn primary" id="amGo">Search</button>
      </div>
      <div class="faint" style="margin-bottom:10px">
        Filtered to ${esc(i.loader || "any loader")}${i.minecraft ? " · MC " + esc(i.minecraft) : ""}.
        Required dependencies are installed automatically.
      </div>
      <div id="amResults"></div>`,
  });

  const run = async () => {
    const host = $("#amResults", m.el);
    host.innerHTML = `<span class="spin"></span> searching…`;
    const params = new URLSearchParams({
      q: $("#amQ", m.el).value.trim(),
      source: $("#amSrc", m.el).value,
      page_size: 20,
    });
    if (i.minecraft) params.set("game_version", i.minecraft);
    if (i.loader) params.set("loader", i.loader);
    try {
      const r = await api("/api/browse/mods?" + params);
      host.innerHTML = r.items.length ? r.items.map((p) => `
        <div class="pack" style="cursor:default">
          ${iconHTML(p.logo, p.name, "mod-icon")}
          <div class="body">
            <div class="title">${esc(p.name)}</div>
            <div class="summary">${esc(p.summary || "")}</div>
            <div class="meta">
              <span class="pill">${num(p.downloads)} ↓</span>
              ${p.server_side ? `<span class="pill ${p.server_side === "unsupported" ? "err" : "ok"}">server: ${esc(p.server_side)}</span>` : ""}
            </div>
          </div>
          <div><button class="btn sm primary" data-pick="${esc(String(p.id))}"
             data-src="${esc(p.source)}" data-name="${esc(p.name)}">Choose</button></div>
        </div>`).join("") : `<div class="empty">Nothing found.</div>`;
      $$("[data-pick]", m.el).forEach((b) => {
        b.onclick = () => pickModVersion(b.dataset.src, b.dataset.pick,
                                         b.dataset.name, m);
      });
    } catch (e) { host.innerHTML = `<div class="banner err">${esc(e.message)}</div>`; }
  };
  $("#amGo", m.el).onclick = run;
  $("#amQ", m.el).addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
  if (prefill) run();
  return m;
}

async function pickModVersion(source, projectId, name, parent) {
  const i = state.inst;
  const host = $("#amResults", parent.el);
  host.innerHTML = `<span class="spin"></span> loading versions…`;
  const params = new URLSearchParams();
  if (i.minecraft) params.set("game_version", i.minecraft);
  if (i.loader) params.set("loader", i.loader);
  try {
    const r = await api(`/api/mods/${source}/${projectId}/versions?` + params);
    if (!r.items.length) {
      host.innerHTML = `<div class="empty">No compatible versions for
        ${esc(i.loader || "")} ${esc(i.minecraft || "")}.</div>`;
      return;
    }
    host.innerHTML = `<h3>${esc(name)} — pick a version</h3>
      <div class="table-wrap"><table>
      <thead><tr><th>Version</th><th>MC</th><th>Loader</th><th>Type</th><th></th></tr></thead>
      <tbody>${r.items.map((f) => `
        <tr><td>${esc(f.display_name || f.version_number)}</td>
          <td class="faint">${esc((f.game_versions || []).slice(0, 3).join(", "))}</td>
          <td class="faint">${esc((f.loaders || []).join(", "))}</td>
          <td><span class="pill ${f.release_type === "release" ? "ok" : "warn"}">${esc(f.release_type || "")}</span></td>
          <td><button class="btn sm primary" data-add="${esc(String(f.file_id))}">Select</button></td>
        </tr>`).join("")}</tbody></table></div>`;
    $$("[data-add]", parent.el).forEach((b) => {
      b.onclick = () => previewDependencies(source, projectId, b.dataset.add,
                                            name, parent);
    });
  } catch (e) { host.innerHTML = `<div class="banner err">${esc(e.message)}</div>`; }
}

async function previewDependencies(source, projectId, fileId, name, parent) {
  const host = $("#amResults", parent.el);
  host.innerHTML = `<span class="spin"></span> resolving dependencies…`;
  try {
    const plan = await api(`/api/instances/${state.inst.id}/mods/resolve`, {
      method: "POST",
      body: { source, project_id: projectId, file_id: fileId },
    });
    const dep = plan.dependencies || [];
    const rows = dep.map((d) => `
      <div class="reviewrow">
        <input type="checkbox" class="depSel" data-pid="${esc(String(d.project_id))}" checked>
        ${iconHTML(d.logo, d.name)}
        <div class="body">
          <div><strong>${esc(d.name)}</strong>
            <span class="faint">${esc(d.version || "")}</span></div>
          <div class="reasons">required by ${esc(d.required_by || name)}
            ${d.size ? " · " + mb(d.size) : ""}</div>
        </div>
      </div>`).join("");

    host.innerHTML = `
      <h3>Ready to install ${esc(name)}</h3>
      ${plan.root ? `<div class="reviewrow">
          ${iconHTML(plan.root.logo, plan.root.name)}
          <div class="body"><div><strong>${esc(plan.root.name)}</strong>
            <span class="faint">${esc(plan.root.version || "")}</span></div>
            <div class="reasons">the mod you selected</div></div></div>` : ""}
      ${dep.length ? `<h3 style="margin-top:14px">Dependencies (${dep.length})</h3>
        <div class="faint" style="margin-bottom:6px">Untick any you already
          handle another way. Leaving a required dependency out usually stops
          the server booting.</div>${rows}`
        : `<div class="faint" style="margin-top:10px">No extra dependencies needed.</div>`}
      ${plan.already_satisfied && plan.already_satisfied.length
        ? `<div class="faint" style="margin-top:10px">${plan.already_satisfied.length}
           dependencies are already installed.</div>` : ""}
      ${(plan.warnings || []).map((w) => `<div class="banner" style="margin-top:10px">${esc(w)}</div>`).join("")}
      <div class="row" style="margin-top:14px;justify-content:flex-end">
        <button class="btn primary" id="depGo">Install ${dep.length ? dep.length + 1 : 1} mod(s)</button>
      </div>`;

    $("#depGo", parent.el).onclick = async () => {
      const skip = $$(".depSel:not(:checked)", parent.el).map((c) => c.dataset.pid);
      try {
        const res = await api(`/api/instances/${state.inst.id}/mods/add`, {
          method: "POST",
          body: { source, project_id: projectId, file_id: fileId,
                  with_dependencies: true, skip_dependencies: skip, name },
        });
        parent.close();
        followJob(res.job_id, `Installing ${name}`, () => loadMods());
      } catch (e) { toast(e.message, "err"); }
    };
  } catch (e) { host.innerHTML = `<div class="banner err">${esc(e.message)}</div>`; }
}

$("#identifyMods").onclick = async () => {
  try {
    const r = await api(`/api/instances/${state.inst.id}/mods/identify`,
                        { method: "POST" });
    followJob(r.job_id, "Identifying mods", () => loadMods());
  } catch (e) { toast(e.message, "err"); }
};

$("#checkUpdates").onclick = async () => {
  const btn = $("#checkUpdates");
  btn.disabled = true;
  try {
    const r = await api(`/api/instances/${state.inst.id}/mods/updates`);
    if (r.note) { toast(r.note); return; }
    const m = modal({
      title: `Updates — ${r.updates.length} of ${r.checked} mods`,
      wide: true,
      body: r.updates.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Mod</th><th>Installed</th><th>Latest</th><th></th></tr></thead>
        <tbody>${r.updates.map((u, idx) => `
          <tr><td>${esc(u.name || u.file)}</td>
            <td class="faint">${esc(u.current_version || "—")}</td>
            <td>${esc(u.latest_version || "—")}</td>
            <td><button class="btn sm primary" data-up="${idx}">Update</button></td>
          </tr>`).join("")}</tbody></table></div>`
        : `<div class="empty">Everything is up to date.</div>`,
    });
    $$("[data-up]", m.el).forEach((b) => {
      b.onclick = async () => {
        const u = r.updates[Number(b.dataset.up)];
        b.disabled = true;
        try {
          const res = await api(`/api/instances/${state.inst.id}/mods/add`, {
            method: "POST",
            body: { source: u.source, project_id: u.project_id,
                    file_id: u.latest_file_id, replace_file: u.file,
                    with_dependencies: true, name: u.name },
          });
          b.textContent = "queued";
          followJob(res.job_id, `Updating ${u.name}`, () => loadMods());
        } catch (e) { toast(e.message, "err"); b.disabled = false; }
      };
    });
  } catch (e) { toast(e.message, "err"); }
  finally { btn.disabled = false; }
};

// --- configs -----------------------------------------------------------

async function loadConfigs() {
  $("#cfgList").innerHTML = `<div class="item"><span class="spin"></span> scanning…</div>`;
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
    ).join("")).join("") || `<div class="item faint">No config files found.</div>`;
  $$("#cfgList .item[data-path]").forEach((el) => {
    el.onclick = () => openConfig(el.dataset.path);
  });
}

async function openConfig(path) {
  state.cfgPath = path;
  renderConfigList();
  $("#cfgPath").textContent = path;
  $("#cfgEditor").value = "loading…";
  $("#cfgSave").disabled = true;
  try {
    const r = await api(
      `/api/instances/${state.inst.id}/configs/read?path=${encodeURIComponent(path)}`);
    $("#cfgEditor").value = r.content;
    $("#cfgSave").disabled = !r.editable;
    $("#cfgStatus").textContent = `${r.lines} lines · ${r.language}`;
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
    toast("Saved " + state.cfgPath, "ok");
    $("#cfgStatus").textContent = "saved";
  } catch (e) { toast(e.message, "err"); }
  finally { btn.disabled = false; }
};
$("#cfgFilter").oninput = renderConfigList;

// --- troubleshooting ---------------------------------------------------

$("#runDiag").onclick = runDiagnostics;

$("#runDeep").onclick = async () => {
  try {
    const r = await api(`/api/instances/${state.inst.id}/deep-scan`, { method: "POST" });
    followJob(r.job_id, "Dependency scan", (d) => {
      if (d.result) renderFindings(d.result.findings,
        `Deep scan — ${d.result.scanned} jars`);
    });
  } catch (e) { toast(e.message, "err"); }
};

$("#runAI").onclick = async () => {
  if (!state.ai.available) {
    toast(state.ai.reason || "AI assistant is not available", "err");
    return;
  }
  try {
    const r = await api(`/api/instances/${state.inst.id}/ai/analyse`,
      { method: "POST", body: {} });
    followJob(r.job_id, "AI analysis", (d) => {
      if (d.result) renderAI(d.result);
    });
  } catch (e) { toast(e.message, "err"); }
};

async function runDiagnostics() {
  const btn = $("#runDiag");
  btn.disabled = true;
  $("#diagOut").innerHTML = `<div class="card"><span class="spin"></span> running checks…</div>`;
  try {
    const r = await api(`/api/instances/${state.inst.id}/diagnose`);
    renderFindings(r.findings, "Checks");
    if (r.log_tail || r.crash_tail) {
      $("#logCard").classList.remove("hidden");
      $("#logPath").textContent = r.crash_path || r.log_path || "";
      $("#logTail").textContent =
        (r.crash_tail ? r.crash_tail + "\n\n--- log ---\n\n" : "") + (r.log_tail || "");
    } else $("#logCard").classList.add("hidden");
  } catch (e) {
    $("#diagOut").innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
  } finally { btn.disabled = false; }
}

let lastFindings = [];

function renderFindings(findings, title) {
  lastFindings = findings || [];
  if (!lastFindings.length) {
    $("#diagOut").innerHTML =
      `<div class="card"><span class="pill ok">All clear</span>
       <span class="faint" style="margin-left:8px">${esc(title)} found no problems.</span></div>`;
    return;
  }
  $("#diagOut").innerHTML = `<div class="card">
    <h3>${esc(title)} — ${lastFindings.length} finding(s)</h3>
    ${lastFindings.map((f, i) => `
      <div class="finding ${esc(f.severity)}">
        <div class="row">
          <div style="flex:1;min-width:0">
            <div class="t">${esc(f.title)}</div>
            <div class="d">${esc(f.detail)}</div>
          </div>
          ${f.fix ? `<button class="btn sm primary" data-fix="${i}">Fix</button>` : ""}
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
        toast("EULA rewritten in the form Crafty expects", "ok"); break;
      case "raise_ram":
        btn.disabled = false;
        showTab("optimize");
        toast("Set the heap size in the Optimize tab");
        return;
      case "set_java":
        await api(`/api/instances/${sid}/fix/java`,
          { method: "POST", body: { minecraft: fix.minecraft } });
        toast("Java version corrected", "ok"); break;
      case "disable_mods":
        if (!confirm(`Disable ${fix.files.length} mod(s)?\n\n` +
                     fix.files.join("\n"))) { btn.disabled = false; return; }
        await api(`/api/instances/${sid}/mods/bulk-toggle`,
          { method: "POST", body: { files: fix.files, enabled: false } });
        toast(`Disabled ${fix.files.length} mod(s)`, "ok"); break;
      case "install_dependency":
        btn.disabled = false;
        return findDependency(fix.mod_id);
      case "fix_versions":
        btn.disabled = false;
        return fixVersions(fix.files);
      case "retry_mod":
        await api(`/api/instances/${sid}/mods/add`, {
          method: "POST",
          body: { source: "curseforge", project_id: fix.project_id,
                  file_id: fix.file_id, with_dependencies: true },
        });
        toast("Re-install queued", "ok"); break;
      case "find_client_only":
        btn.disabled = false;
        showTab("mods");
        $("#modFilter").value = "";
        toast("Mods flagged 'client?' are the likely culprits");
        return;
      case "edit_file":
        btn.disabled = false;
        showTab("configs");
        setTimeout(() => openConfig(fix.path), 400);
        return;
      default:
        toast("No automatic fix for this one yet"); btn.disabled = false; return;
    }
    btn.textContent = "Done";
    setTimeout(runDiagnostics, 900);
  } catch (e) { toast(e.message, "err"); btn.disabled = false; }
}

async function fixVersions(files) {
  const m = modal({
    title: "Mods on the wrong game version",
    wide: true,
    body: `<div id="fvBody"><span class="spin"></span> finding compatible builds…</div>`,
  });
  try {
    const r = await api(`/api/instances/${state.inst.id}/fix/versions`,
      { method: "POST", body: { files } });
    const rows = (r.suggestions || []).map((s, i) => `
      <div class="reviewrow">
        <input type="checkbox" class="fvSel" data-i="${i}" checked>
        ${iconHTML(s.logo, s.name)}
        <div class="body">
          <div><strong>${esc(s.name || s.file)}</strong></div>
          <div class="reasons">${esc(s.current_version || s.file)}
            &nbsp;→&nbsp; <span class="pill ok">${esc(s.suggested_version)}</span></div>
        </div>
      </div>`).join("");
    $("#fvBody", m.el).innerHTML = `
      ${rows ? `<div class="faint" style="margin-bottom:8px">Compatible builds for
        ${esc(r.loader || "")} ${esc(r.minecraft || "")}:</div>${rows}
        <div class="row" style="justify-content:flex-end;margin-top:14px">
          <button class="btn primary" id="fvGo">Switch selected</button></div>`
        : `<div class="empty">No automatic replacements found.</div>`}
      ${(r.unresolved || []).length ? `<h3 style="margin-top:16px">Needs a manual look</h3>` +
        r.unresolved.map((u) => `<div class="faint">• ${esc(u.file)} — ${esc(u.why)}</div>`).join("")
        : ""}`;

    const go = $("#fvGo", m.el);
    if (go) go.onclick = async () => {
      const picks = $$(".fvSel:checked", m.el).map((c) => r.suggestions[Number(c.dataset.i)]);
      go.disabled = true;
      for (const s of picks) {
        try {
          await api(`/api/instances/${state.inst.id}/mods/add`, {
            method: "POST",
            body: { source: s.source, project_id: s.project_id,
                    file_id: s.suggested_file_id, replace_file: s.file,
                    with_dependencies: false },
          });
          toast(`${s.name} → ${s.suggested_version}`, "ok");
        } catch (e) { toast(`${s.name}: ${e.message}`, "err"); }
      }
      m.close(); loadMods(); runDiagnostics();
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
    title: "Install missing dependency: " + modId,
    wide: true,
    body: `<div id="depBody"><span class="spin"></span> searching…</div>`,
  });
  try {
    const r = await api(`/api/diagnose/dependency/${encodeURIComponent(modId)}?` + params);
    const section = (label, items) => (items && items.length)
      ? `<h3>${label}</h3>` + items.map((p) => `
        <div class="pack" style="cursor:default">
          ${iconHTML(p.logo, p.name, "mod-icon")}
          <div class="body"><div class="title">${esc(p.name)}</div>
            <div class="summary">${esc(p.summary || "")}</div></div>
          <div><button class="btn sm primary" data-dep="${esc(String(p.id))}"
            data-src="${esc(p.source)}" data-name="${esc(p.name)}">Choose</button></div>
        </div>`).join("") : "";
    $("#depBody", m.el).innerHTML =
      section("CurseForge", r.curseforge) + section("Modrinth", r.modrinth) ||
      `<div class="empty">No candidates found — it may be bundled inside another mod.</div>`;
    $$("[data-dep]", m.el).forEach((b) => {
      b.onclick = () => {
        m.close();
        const parent = openAddMod(modId);
        setTimeout(() => pickModVersion(b.dataset.src, b.dataset.dep,
                                        b.dataset.name, parent), 60);
      };
    });
  } catch (e) {
    $("#depBody", m.el).innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
  }
}

// --- AI panel ----------------------------------------------------------

function renderAI(result) {
  const host = $("#aiOut");
  if (!result.available) {
    host.innerHTML = `<div class="banner">AI assistant unavailable —
      ${esc(result.reason || "")}${result.hint ? `<div class="faint" style="margin-top:6px">${esc(result.hint)}</div>` : ""}</div>`;
    return;
  }
  if (!result.ok) {
    host.innerHTML = `<div class="banner err">AI analysis failed — ${esc(result.error || "")}
      ${result.partial ? `<div class="faint" style="margin-top:8px">Partial output before it stopped:</div>
        <pre class="log" style="margin-top:4px">${esc(result.partial)}</pre>` : ""}
      ${result.raw ? `<pre class="log" style="margin-top:8px">${esc(result.raw)}</pre>` : ""}</div>`;
    return;
  }
  const conf = { high: "ok", medium: "warn", low: "" }[result.confidence] || "";
  host.innerHTML = `<div class="ai-card">
    <div class="row" style="margin-bottom:6px">
      <strong>AI assessment</strong>
      <span class="pill ${conf}">${esc(result.confidence)} confidence</span>
      <span class="pill">${esc(result.model || "")}</span>
      <div class="spacer"></div>
      ${result.actions.length
        ? `<button class="btn sm primary" id="aiApplyAll">Apply selected</button>` : ""}
    </div>
    <div>${esc(result.summary || "")}</div>
    ${result.notes ? `<div class="faint" style="margin-top:8px">${esc(result.notes)}</div>` : ""}
    ${result.actions.length ? result.actions.map((a, i) => `
      <div class="ai-action">
        <input type="checkbox" class="aiSel" data-i="${i}" ${a.major ? "" : "checked"}>
        <div class="body">
          <div><strong>${esc(a.description)}</strong>
            ${a.major ? '<span class="pill warn">needs confirmation</span>'
                      : '<span class="pill ok">safe</span>'}</div>
          <div class="faint">${esc(a.why)}</div>
          <div class="faint mono" style="font-size:11.5px;margin-top:3px">
            ${esc(a.action)}(${esc(JSON.stringify(a.args))})</div>
        </div>
      </div>`).join("")
      : `<div class="faint" style="margin-top:10px">No actions proposed.</div>`}
    ${result.rejected && result.rejected.length ? `<div class="faint" style="margin-top:10px">
      Discarded ${result.rejected.length} suggestion(s) that referenced things
      not present in this instance.</div>` : ""}
    <div class="faint" style="margin-top:10px">
      A small local model wrote this. Read each action before applying it.</div>
  </div>`;

  const applyBtn = $("#aiApplyAll");
  if (applyBtn) applyBtn.onclick = async () => {
    const picked = $$(".aiSel:checked").map((c) => result.actions[Number(c.dataset.i)]);
    if (!picked.length) { toast("Nothing selected"); return; }
    const major = picked.filter((a) => a.major);
    if (major.length) {
      const list = major.map((a) =>
        `• ${a.description} ${JSON.stringify(a.args)}`).join("\n");
      if (!confirm(`These change or remove content:\n\n${list}\n\nApply them?`)) return;
    }
    applyBtn.disabled = true;
    try {
      const res = await api(`/api/instances/${state.inst.id}/ai/apply`, {
        method: "POST", body: { actions: picked, confirmed: true },
      });
      toast(`Applied ${res.applied.length} action(s)`, "ok");
      (res.failed || []).forEach((f) => toast(`${f.action}: ${f.error}`, "err"));
      loadMods(); runDiagnostics();
    } catch (e) { toast(e.message, "err"); }
    finally { applyBtn.disabled = false; }
  };
}

// --- optimize ----------------------------------------------------------

async function loadOptimize() {
  const host = $("#optOut");
  host.innerHTML = `<div class="card"><span class="spin"></span> measuring…</div>`;
  try {
    const p = await api(`/api/instances/${state.inst.id}/optimize`);
    renderOptimize(p);
  } catch (e) { host.innerHTML = `<div class="banner err">${esc(e.message)}</div>`; }
}

function renderOptimize(p) {
  const h = p.host, m = p.memory;
  const pct = h.total_ram_gb ? Math.min(100, (m.heap_gb / h.total_ram_gb) * 100) : 0;
  const groups = {};
  p.flags.forEach((f, i) => (groups[f.group] = groups[f.group] || []).push({ ...f, i }));

  $("#optOut").innerHTML = `
    <div class="card">
      <h3>This machine</h3>
      <div class="statgrid">
        <div class="stat"><div class="k">Total RAM</div><div class="v">${h.total_ram_gb} GB</div></div>
        <div class="stat"><div class="k">Available now</div><div class="v">${h.available_ram_gb} GB</div></div>
        <div class="stat"><div class="k">CPU threads</div><div class="v">${h.cpu_count}</div></div>
        <div class="stat"><div class="k">Mods</div><div class="v">${p.mod_count}</div></div>
      </div>
      ${h.cpu_model ? `<div class="faint">${esc(h.cpu_model)}</div>` : ""}
      ${h.note ? `<div class="banner" style="margin-top:10px">${esc(h.note)}</div>` : ""}
    </div>

    <div class="card">
      <div class="row" style="margin-bottom:8px">
        <h3 style="margin:0">Memory</h3>
        <div class="spacer"></div>
        <span class="faint">based on ${esc(m.basis)}</span>
      </div>
      <div class="row" style="margin-bottom:10px">
        <label>Heap size (GB)
          <input type="number" id="optHeap" min="1" max="64" step="0.5"
            value="${m.heap_gb}" style="width:100px">
        </label>
        <div style="flex:1">
          <div class="meter"><div style="width:${pct}%"></div>
            <span>${m.heap_gb} GB of ${h.total_ram_gb} GB</span></div>
        </div>
      </div>
      <div class="faint">Pack asked for ${m.requested_gb} GB ·
        safe ceiling here is ${m.ceiling_gb} GB (${m.reserve_gb} GB reserved for
        the OS and other services).</div>
      ${m.warnings.map((w) => `<div class="banner" style="margin-top:10px">${esc(w)}</div>`).join("")}
      ${p.current.exists ? `<div class="faint" style="margin-top:10px">
        Currently: -Xms${p.current.xms_mb}M / -Xmx${p.current.xmx_mb}M with
        ${p.current.flags.length} flags.</div>` : ""}
      ${p.note ? `<div class="banner" style="margin-top:10px">${esc(p.note)}</div>` : ""}
    </div>

    <div class="card">
      <div class="row" style="margin-bottom:6px">
        <h3 style="margin:0">JVM flags</h3>
        <div class="spacer"></div>
        <button class="btn sm" id="optAll">Select all</button>
        <button class="btn sm" id="optNone">Select none</button>
        <button class="btn sm" id="optRec">Reset to recommended</button>
      </div>
      <div class="faint" style="margin-bottom:8px">Each one is independent —
        untick anything you would rather not run.</div>
      ${Object.entries(groups).map(([group, items]) => `
        <div class="group" style="margin-top:8px">${esc(group)}</div>
        ${items.map((f) => `
          <div class="optrow">
            <label class="switch">
              <input type="checkbox" class="flagSel" data-flag="${esc(f.flag)}"
                data-rec="${f.recommended}" ${f.enabled ? "checked" : ""}>
              <span class="track"></span>
            </label>
            <div class="body">
              <div><strong>${esc(f.label)}</strong>
                ${f.applied ? '<span class="pill ok">active</span>' : ""}</div>
              <div class="flagname">${esc(f.flag)}</div>
              <div class="why">${esc(f.why)}</div>
            </div>
          </div>`).join("")}`).join("")}
    </div>

    <div class="card">
      <h3>server.properties</h3>
      <div class="faint" style="margin-bottom:8px">Performance-related settings only.</div>
      ${p.properties.map((prop) => `
        <div class="optrow">
          <label class="switch">
            <input type="checkbox" class="propSel" data-key="${esc(prop.key)}"
              data-value="${esc(prop.value)}" ${prop.enabled ? "checked" : ""}>
            <span class="track"></span>
          </label>
          <div class="body">
            <div><strong>${esc(prop.key)} = ${esc(prop.value)}</strong>
              ${prop.applied ? '<span class="pill ok">active</span>'
                : prop.current != null ? `<span class="pill">now: ${esc(prop.current)}</span>` : ""}</div>
            <div class="why">${esc(prop.why)}</div>
          </div>
        </div>`).join("")}
    </div>

    <div class="card row">
      <div class="faint">Changes take effect on the next restart.</div>
      <div class="spacer"></div>
      <button class="btn primary" id="optApply">Apply selected</button>
    </div>`;

  $("#optAll").onclick = () => $$(".flagSel").forEach((c) => (c.checked = true));
  $("#optNone").onclick = () => $$(".flagSel").forEach((c) => (c.checked = false));
  $("#optRec").onclick = () =>
    $$(".flagSel").forEach((c) => (c.checked = c.dataset.rec === "true"));

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
          flags, properties, xms_equals_xmx: true,
        },
      });
      res.applied.forEach((a) => toast(a, "ok"));
      (res.skipped || []).forEach((s) => toast(`${s.what}: ${s.why}`, "err"));
      loadOptimize();
    } catch (e) { toast(e.message, "err"); }
    finally { btn.disabled = false; }
  };
}

// --- modpack tab -------------------------------------------------------

async function loadPackTab() {
  const i = state.inst;
  const pack = i.manifest.pack;
  const host = $("#packOut");
  if (!pack) {
    host.innerHTML = `<div class="card"><div class="empty">
      This instance was not installed by BlessForge, so there is no pack to
      track. You can still manage its mods and configs.</div></div>`;
    return;
  }
  host.innerHTML = `<div class="card">
      <h3>${esc(pack.name || "")}</h3>
      <div class="faint">Installed version <span class="pill accent">${esc(pack.version || "?")}</span>
        · from ${esc(pack.install_source === "server_pack" ? "the official server pack" : "the pack manifest")}
        · ${esc(i.loader || "")} ${esc(i.minecraft || "")}</div>
      ${(i.manifest.excluded_mods || []).length ? `<div class="faint" style="margin-top:8px">
        ${i.manifest.excluded_mods.length} mods were excluded at install time.</div>` : ""}
      ${(i.manifest.problems || []).length ? `<div class="banner" style="margin-top:10px">
        ${i.manifest.problems.length} mods failed to install. See Troubleshoot.</div>` : ""}
      <div class="row" style="margin-top:12px">
        <button class="btn primary" id="switchPack">Switch modpack version</button>
      </div>
    </div>
    <div id="packVersions"></div>`;

  $("#switchPack").onclick = async () => {
    const box = $("#packVersions");
    box.innerHTML = `<div class="card"><span class="spin"></span> loading versions…</div>`;
    try {
      const r = await api(`/api/modpacks/${pack.project_id}/files?page_size=50`);
      box.innerHTML = `<div class="card">
        <h3>Choose a version</h3>
        <div class="banner">The world is kept. Mods and configs are replaced,
          so anything you changed by hand will be overwritten.</div>
        <div class="table-wrap"><table>
          <thead><tr><th>Version</th><th>MC</th><th>Source</th><th></th></tr></thead>
          <tbody>${r.items.map((f) => `
            <tr><td>${esc(f.display_name)}
              ${String(f.file_id) === String(pack.file_id) ? '<span class="pill accent">current</span>' : ""}</td>
              <td class="faint">${esc((f.game_versions || []).join(", "))}</td>
              <td>${f.server_pack_file_id ? '<span class="pill ok">server pack</span>'
                                          : '<span class="pill warn">manifest</span>'}</td>
              <td>${String(f.file_id) === String(pack.file_id) ? "" :
                `<button class="btn sm primary" data-pk="${esc(String(f.file_id))}">Switch</button>`}</td>
            </tr>`).join("")}</tbody></table></div></div>`;
      $$("[data-pk]").forEach((b) => {
        b.onclick = async () => {
          if (!confirm("Replace all mods and configs with this version?")) return;
          b.disabled = true;
          try {
            const res = await api(`/api/instances/${i.id}/switch-pack-version`, {
              method: "POST",
              body: { mod_id: pack.project_id, file_id: Number(b.dataset.pk) },
            });
            followJob(res.job_id, "Switching pack version", () => openInstance(i.id));
          } catch (e) { toast(e.message, "err"); b.disabled = false; }
        };
      });
    } catch (e) { box.innerHTML = `<div class="banner err">${esc(e.message)}</div>`; }
  };
}

// --- browse & install --------------------------------------------------

async function searchPacks(reset = true) {
  if (reset) { state.packIndex = 0; $("#packs").innerHTML = ""; }
  const params = new URLSearchParams({
    q: $("#q").value.trim(), sort: $("#sort").value,
    index: state.packIndex, page_size: 30,
  });
  if ($("#mcv").value) params.set("game_version", $("#mcv").value);
  if ($("#loader").value) params.set("loader", $("#loader").value);
  $("#searchBtn").disabled = true;
  try {
    const r = await api("/api/browse/modpacks?" + params);
    const host = $("#packs");
    r.items.forEach((p) => {
      const el = document.createElement("div");
      el.className = "pack";
      const vers = [...new Set((p.latest_files || []).flatMap((f) => f.game_versions))].slice(0, 3);
      el.innerHTML = `${iconHTML(p.logo, p.name, "mod-icon")
          .replace("mod-icon", "mod-icon")}
        <div class="body">
          <div class="title">${esc(p.name)}</div>
          <div class="summary">${esc(p.summary || "")}</div>
          <div class="meta"><span class="pill">${num(p.downloads)} ↓</span>
            ${vers.map((v) => `<span class="pill info">${esc(v)}</span>`).join("")}</div>
        </div>`;
      el.onclick = () => openPack(p);
      host.appendChild(el);
    });
    state.packIndex += r.items.length;
    $("#morePacks").classList.toggle("hidden", r.items.length < 30);
    if (!r.items.length && reset) {
      $("#packs").innerHTML = `<div class="empty">No modpacks matched.</div>`;
    }
  } catch (e) { toast(e.message, "err"); }
  finally { $("#searchBtn").disabled = false; }
}

async function openPack(pack) {
  const m = modal({
    title: pack.name, wide: true,
    body: `<div class="row" style="margin-bottom:12px">
        ${iconHTML(pack.logo, pack.name, "mod-icon")}
        <div><div class="muted">${esc(pack.summary || "")}</div>
          <div class="faint" style="margin-top:6px">${num(pack.downloads)} downloads ·
            <a href="${esc(pack.url || "#")}" target="_blank" rel="noopener">CurseForge page</a>
          </div></div></div>
      <h3>Choose a version</h3>
      <div id="verList"><span class="spin"></span> loading…</div>`,
  });
  try {
    const r = await api(`/api/modpacks/${pack.id}/files?page_size=50`);
    $("#verList", m.el).innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>Version</th><th>MC</th><th>Loader</th><th>Type</th>
        <th>Source</th><th></th></tr></thead>
      <tbody>${r.items.map((f, idx) => `
        <tr><td>${esc(f.display_name)}</td>
          <td class="faint">${esc((f.game_versions || []).join(", "))}</td>
          <td class="faint">${esc((f.loaders || []).join(", "))}</td>
          <td><span class="pill ${f.release_type === "release" ? "ok" : "warn"}">${esc(f.release_type)}</span></td>
          <td>${f.server_pack_file_id
            ? '<span class="pill ok" title="Ready-made server build">server pack</span>'
            : '<span class="pill warn" title="Assembled from the manifest">manifest</span>'}</td>
          <td><button class="btn sm primary" data-i="${idx}">Install</button></td>
        </tr>`).join("")}</tbody></table></div>`;
    $$("[data-i]", m.el).forEach((b) => {
      b.onclick = () => { m.close(); installWizard(pack, r.items[Number(b.dataset.i)]); };
    });
  } catch (e) {
    $("#verList", m.el).innerHTML = `<div class="banner err">${esc(e.message)}</div>`;
  }
}

function installWizard(pack, file) {
  const m = modal({
    title: "Install " + pack.name,
    body: `<div class="faint" style="margin-bottom:12px">${esc(file.display_name)}</div>
      <div class="row" style="margin-bottom:10px">
        <label style="flex:1">Server name<br>
          <input type="text" id="iName" value="${esc(pack.name.slice(0, 60))}" style="width:100%"></label>
        <label>Port<br><input type="number" id="iPort" value="25565" style="width:110px"></label>
      </div>
      <label class="check" style="margin-bottom:6px">
        <input type="checkbox" id="iServerPack" ${file.server_pack_file_id ? "checked" : "disabled"}>
        Prefer the official server build
        ${file.server_pack_file_id ? "" : "(none for this version)"}
      </label>
      <label class="check" style="margin-bottom:6px">
        <input type="checkbox" id="iReview" checked>
        Review client-only mods before they are removed
      </label>
      <label class="check">
        <input type="checkbox" id="iOptimize" checked>
        Tune memory and JVM flags for this machine
      </label>
      <div class="faint" style="margin-top:12px">
        ${file.server_pack_file_id
          ? "The server build is installed over a matching loader."
          : "No server build exists, so the manifest is resolved and every server-side mod fetched individually."}
      </div>`,
    actions: [
      { label: "Cancel", onClick: (mm) => mm.close() },
      { label: "Continue", cls: "primary", onClick: async (mm, btn) => {
          btn.disabled = true;
          const opts = {
            mod_id: pack.id, file_id: file.file_id,
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
      body: { mod_id: opts.mod_id, file_id: opts.file_id,
              prefer_server_pack: opts.prefer_server_pack },
    });
    followJob(r.job_id, "Analysing " + pack.name, (d) => {
      if (d.result) showReview(pack, opts, d.result);
    });
  } catch (e) { toast(e.message, "err"); }
}

function showReview(pack, opts, analysis) {
  const review = analysis.review || {};
  const items = review.candidates || [];
  const mem = analysis.memory || {};

  const rows = items.map((c, i) => {
    const keep = c.recommendation === "keep";
    const remove = c.recommendation === "remove";
    return `<div class="reviewrow ${keep ? "keep" : ""}">
      <input type="checkbox" class="rvSel" data-i="${i}"
        data-file="${esc(c.file_name)}" ${remove ? "checked" : ""}>
      ${iconHTML(c.logo, c.name)}
      <div class="body">
        <div><strong>${esc(c.name || c.file_name)}</strong>
          ${remove ? '<span class="pill err">client-only</span>' : ""}
          ${c.recommendation === "review" ? '<span class="pill warn">uncertain</span>' : ""}
          ${keep ? '<span class="pill ok">keep — other mods need it</span>' : ""}
        </div>
        <div class="reasons">${esc((c.reasons || []).join(" · "))}</div>
        ${c.required_by_others ? `<div class="reasons">required by
          ${esc(c.required_by_others.join(", "))}</div>` : ""}
        <div class="mod-sub">${esc(c.file_name)}</div>
      </div>
    </div>`;
  }).join("");

  const m = modal({
    title: "Review before installing",
    wide: true,
    body: `
      <div class="statgrid">
        <div class="stat"><div class="k">Pack</div><div class="v" style="font-size:14px">${esc(analysis.pack.name || pack.name)}</div></div>
        <div class="stat"><div class="k">Loader</div><div class="v" style="font-size:14px">${esc(analysis.loader)} ${esc(analysis.minecraft)}</div></div>
        <div class="stat"><div class="k">Mods</div><div class="v">${review.total_mods ?? "—"}</div></div>
        <div class="stat"><div class="k">Heap</div><div class="v">${mem.heap_gb ?? "—"} GB</div></div>
      </div>
      ${(mem.warnings || []).map((w) => `<div class="banner">${esc(w)}</div>`).join("")}
      ${(analysis.warnings || []).map((w) => `<div class="banner">${esc(w)}</div>`).join("")}

      <h3 style="margin-top:14px">Possible client-only mods (${items.length})</h3>
      <div class="faint" style="margin-bottom:10px">
        Ticked mods will <b>not</b> be installed. Anything another mod depends on
        is left unticked on purpose — removing those is what turns a working
        pack into a missing-dependency crash.
      </div>
      ${items.length ? rows : `<div class="empty">Nothing looks client-only.</div>`}`,
    actions: [
      { label: "Cancel", onClick: (mm) => mm.close() },
      { label: "Install", cls: "primary", onClick: (mm, btn) => {
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
      loadInstances(); showView("instances");
    });
  } catch (e) { toast(e.message, "err"); }
}

// --- jobs --------------------------------------------------------------

function followJob(jobId, title, onDone) {
  const m = modal({
    title,
    body: `<div class="row" style="margin-bottom:4px">
             <div id="jStep" class="muted">Starting…</div>
             <div class="spacer"></div>
             <span class="faint" id="jClock"></span>
           </div>
           <div class="bar"><div id="jBar"></div></div>
           <div class="hidden" id="jStreamWrap">
             <div class="row" style="margin:8px 0 4px">
               <strong class="faint">Model output</strong>
               <div class="spacer"></div>
               <span class="faint" id="jStreamMeta"></span>
             </div>
             <div class="log stream" id="jStream"></div>
           </div>
           <div class="log" id="jLog"></div>`,
    actions: [{ label: "Run in background", onClick: (mm) => mm.close() }],
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
    // Keep the pane pinned to the bottom unless the user scrolled up to read.
    const pinned = streamEl.scrollHeight - streamEl.scrollTop
                   - streamEl.clientHeight < 40;
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
      es.close(); stop();
      if (d.status === "done") {
        stepEl.innerHTML = `<span class="pill ok">Finished</span>`;
        barEl.style.width = "100%";
        toast(title + " — done", "ok");
        // Close automatically only when a follow-up view renders the result.
        if (onDone) { m.close(); onDone(d); }
      } else if (d.status === "error") {
        stepEl.innerHTML = `<span class="pill err">Failed</span> ${esc(d.error || "")}`;
        toast(title + " — failed", "err");
      }
    }
  };
  es.onerror = () => { es.close(); stop(); };
  return m;
}

async function loadJobs() {
  try {
    const r = await api("/api/jobs");
    $("#jobList").innerHTML = r.items.length ? r.items.map((j) => `
      <div class="card">
        <div class="row">
          <strong>${esc(j.title)}</strong>
          <span class="pill ${j.status === "done" ? "ok" : j.status === "error" ? "err" : "info"}">${esc(j.status)}</span>
          <div class="spacer"></div>
          <span class="faint">${esc(j.step || "")}${j.percent ? " · " + j.percent + "%" : ""}</span>
        </div>
        ${j.error ? `<div class="faint" style="color:var(--red);margin-top:6px">${esc(j.error)}</div>` : ""}
      </div>`).join("") : `<div class="empty">Nothing has run yet.</div>`;
  } catch (e) { toast(e.message, "err"); }
}

// --- boot --------------------------------------------------------------

$("#searchBtn").onclick = () => searchPacks(true);
$("#q").addEventListener("keydown", (e) => { if (e.key === "Enter") searchPacks(true); });
$("#morePacks").onclick = () => searchPacks(false);
$("#refreshInstances").onclick = loadInstances;

(async function boot() {
  await checkHealth();
  try {
    const v = await api("/api/meta/minecraft-versions");
    $("#mcv").innerHTML = `<option value="">Any MC version</option>` +
      v.items.slice(0, 60).map((x) => `<option>${esc(x)}</option>`).join("");
  } catch { /* CurseForge may not be configured yet */ }
  await loadInstances();
  setInterval(checkHealth, 60000);
})();

})();
