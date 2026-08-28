// Covers what this session added: the Terminal tab and its stream, the
// install dialog's client-only decisions, the Activity instance chip, the
// client-side tag on the mod list, and the dependency view.
//
// Every write is intercepted; nothing is installed, toggled or sent to a
// real server. The console stream is driven by a fake EventSource, so this
// runs against a stopped instance just as well as a running one.
import { JSDOM, VirtualConsole } from "jsdom";
import fs from "fs";

const STATIC = process.env.BF_STATIC || "/static";
const BASE = process.env.BF_URL || "http://127.0.0.1:8710";
const SID = process.env.BF_SERVER_ID;

const html = fs.readFileSync(STATIC + "/index.html", "utf8");
const appjs = fs.readFileSync(STATIC + "/app.js", "utf8");
const errors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errors.push("jsdomError: " + (e.stack || e.message)));
vc.on("error", (...a) => errors.push("console.error: " + a.join(" ")));
const dom = new JSDOM(html.replace(/<script src="\/static\/app.js"><\/script>/, ""), {
  url: BASE + "/", runScripts: "dangerously", pretendToBeVisual: true, virtualConsole: vc,
});
const { window } = dom;

const sources = [];
window.EventSource = class {
  constructor(u) { this.url = u; this.onmessage = null; this.onerror = null; sources.push(this); }
  emit(o) { if (this.onmessage) this.onmessage({ data: JSON.stringify(o) }); }
  close() { this.closed = true; }
};

const writes = [];
let fakeJobs = null;
let fakeMods = null;
const json = (o) => new globalThis.Response(JSON.stringify(o),
  { status: 200, headers: { "Content-Type": "application/json" } });
window.fetch = async (url, opts = {}) => {
  const path = String(url);
  const method = (opts.method || "GET").toUpperCase();
  if (method !== "GET") {
    writes.push({ path, body: opts.body ? JSON.parse(opts.body) : null });
    if (path.includes("/install/") || path.includes("/ai/") || path.includes("/deep-scan")) {
      return json({ job_id: "job-fake-1" });
    }
    return json({ ok: true });
  }
  if (fakeJobs && path.startsWith("/api/jobs") && !path.includes("/events")) {
    return json({ items: fakeJobs });
  }
  if (fakeMods && /\/mods(\?|$)/.test(path)) {
    return json({ directory: "mods", count: fakeMods.length,
                  enabled: fakeMods.filter((m) => m.enabled).length,
                  client_only: fakeMods.filter((m) => m.client_only).length,
                  mods: fakeMods });
  }
  if (path.includes("/mods/dependencies")) {
    return json({
      count: 3, standalone: 1, note: null,
      parents: [{ file: "create.jar", name: "Create", enabled: true, present: true,
                  dependencies: [{ file: "flywheel.jar", name: "Flywheel",
                                   enabled: true, present: true }] }],
      orphans: [{ file: "lost.jar", name: "Lost lib", enabled: true, present: true,
                  required_by: "Something" }],
    });
  }
  return globalThis.fetch(path.startsWith("http") ? path : BASE + path, opts);
};
window.scrollTo = () => {};
window.HTMLElement.prototype.scrollIntoView = () => {};

const s = window.document.createElement("script");
s.textContent = appjs;
window.document.body.appendChild(s);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const $ = (q) => window.document.querySelector(q);
const $$ = (q) => [...window.document.querySelectorAll(q)];
const out = [];
const check = (n, c, extra = "") => { out.push(!!c); console.log(`${c ? "PASS" : "FAIL"}  ${n}${extra ? "  — " + extra : ""}`); };

await sleep(6000);
await window.openInstance(SID);
await sleep(5000);

// ---------- terminal ----------
check("terminal tab button exists", !!$('#instTabs [data-tab="terminal"]'));
$('#instTabs [data-tab="terminal"]').click();
await sleep(400);
check("terminal pane active", $("#tab-terminal").classList.contains("active"));

const stream = sources.find((x) => x.url.includes("/console/stream"));
check("console stream opened", !!stream, stream ? stream.url : "none");

stream.emit({ event: "lines", running: true, source: "buffer", lines: [
  "[10:00:00] [Server thread/INFO]: Starting minecraft server",
  "[10:00:01] [Server thread/WARN]: Something looks odd",
  "[10:00:02] [Server thread/ERROR]: java.lang.NullPointerException",
]});
await sleep(200);
check("lines rendered", $$("#termOut .term-line").length === 3,
      $$("#termOut .term-line").length + " lines");
check("warn line classified", !!$("#termOut .term-line.warn"));
check("error line classified", !!$("#termOut .term-line.err"));
check("status reflects running", $("#termState").textContent.includes("running"));
check("source pill names the buffer", $("#termSource").textContent === "live console");
check("command input enabled while running", !$("#termCmd").disabled);

// A log line containing markup must never become markup.
stream.emit({ event: "lines", running: true, source: "buffer",
              lines: ["<img src=x onerror=alert(1)> <player> joined"] });
await sleep(150);
check("console output is escaped, not parsed",
      $$("#termOut img").length === 0 &&
      $("#termOut").textContent.includes("<img src=x"));

$("#termFilter").value = "NullPointer";
$("#termFilter").dispatchEvent(new window.Event("input", { bubbles: true }));
await sleep(150);
check("filter narrows the console", $$("#termOut .term-line").length === 1,
      $$("#termOut .term-line").length + " shown");
$("#termFilter").value = "";
$("#termFilter").dispatchEvent(new window.Event("input", { bubbles: true }));
await sleep(150);
check("clearing the filter restores every line", $$("#termOut .term-line").length === 4);

$("#termCmd").value = "list";
$("#termForm").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
await sleep(300);
const sent = writes.find((w) => w.path.includes("/command"));
check("command posted to the backend", !!sent && sent.body.command === "list",
      sent ? JSON.stringify(sent.body) : "none");
check("command echoed immediately", !!$("#termOut .term-line.cmd"));
check("input cleared after send", $("#termCmd").value === "");

stream.emit({ event: "idle", running: false, source: "file" });
await sleep(150);
check("stopped server disables the command box", $("#termCmd").disabled);
check("hint explains why", $("#termHint").textContent.includes("running server"));

$("#termClear").click();
await sleep(120);
check("clear empties the console", $$("#termOut .term-line").length === 0);

// Leaving the tab must close the stream, or it polls Crafty forever.
$('#instTabs [data-tab="mods"]').click();
await sleep(400);
check("leaving the terminal closes its stream", stream.closed === true);

// ---------- mod list: client-side tagging ----------
// The mod list is fed from the API, so the fixture goes in through fetch and
// the app renders it by its own code path -- no internal render call.
function renderNow() {
  const ev = new window.Event("input", { bubbles: true });
  $("#modFilter").dispatchEvent(ev);
}
fakeMods = [
  { file: "figura-0.1.5.jar.disabled", enabled: false, name: "Figura", identified: true,
    client_only: true, client_only_reasons: ["Modrinth lists server_side: unsupported"],
    dependencies: [] },
  { file: "create-0.5.1.jar", enabled: true, name: "Create", identified: true,
    client_only: false, dependencies: ["flywheel.jar"] },
];
await window.__bf.loadMods();
await sleep(300);
const figuraRow = $$('#modList .modrow').find((r) => r.dataset.file.startsWith("figura"));
check("client-only mod tagged client-side",
      !!figuraRow && figuraRow.textContent.includes("client-side"));
check("tag carries the evidence as a tooltip",
      !!figuraRow && /server_side/.test(figuraRow.querySelector(".pill.warn").title));
check("disabled client mod says it can be re-enabled",
      !!figuraRow && figuraRow.querySelector(".mod-sub").textContent.includes("safe to re-enable"));
const createRow = $$('#modList .modrow').find((r) => r.dataset.file.startsWith("create"));
check("a mod with dependencies shows a count", !!createRow && createRow.textContent.includes("+1"));

$("#modState").value = "client";
renderNow();
await sleep(200);
check("client-side filter narrows the list", $$("#modList .modrow").length === 1);
$("#modState").value = "all";
renderNow();

// ---------- dependency view ----------
$("#modDeps").click();
await sleep(600);
check("dependency modal opens", !!$(".modal-back"));
check("parent mod listed", $(".modal").textContent.includes("Create pulled in 1"));
check("dependency listed", $(".modal").textContent.includes("Flywheel"));
check("orphaned dependency section shown",
      $(".modal").textContent.includes("Installed as dependencies"));
$(".modal .close").click();
await sleep(200);

// ---------- install decisions ----------
writes.length = 0;
window.__bf.showReview(
  { name: "Test Pack" },
  { mod_id: 1, file_id: 2, server_name: "Test", port: 25599 },
  { pack: { name: "Test Pack" }, loader: "fabric", minecraft: "1.20.1", memory: {},
    review: { total_mods: 2, candidates: [
      { file_name: "figura-0.1.5.jar", name: "Figura", recommendation: "remove",
        reasons: ["Modrinth lists this mod as server_side: unsupported"],
        modrinth_url: "https://modrinth.com/mod/figura" },
      { file_name: "sodium.jar", name: "Sodium", recommendation: "keep", reasons: ["needed"] },
    ] } });
await sleep(400);
check("review says mods are disabled, not deleted",
      /disabled/i.test($(".modal").textContent) && !/excluded from the server install/i.test($(".modal").textContent));
check("modrinth evidence linked", !!$('.modal a[href*="modrinth.com"]'));
$$(".modal .btn").find((b) => /Proceed/.test(b.textContent)).click();
await sleep(400);
const install = writes.find((w) => w.path.includes("/install/modpack"));
check("install sends disable_files, not exclude_files",
      !!install && Array.isArray(install.body.disable_files) && !install.body.exclude_files,
      install ? JSON.stringify(install.body.disable_files) : "no install call");
check("the flagged jar is the one disabled",
      !!install && install.body.disable_files.includes("figura-0.1.5.jar"));
check("the evidence travels with the decision",
      !!install && !!install.body.client_reasons &&
      !!install.body.client_reasons["figura-0.1.5.jar"]);
check("the port typed at install is sent", !!install && install.body.port === 25599);

// ---------- activity names its instance ----------
const jobsHost = $("#jobList");
jobsHost.innerHTML = "";
fakeJobs = [{ id: "j1", title: "Dependency scan", status: "running",
              step: "Reading jars", percent: 40, error: null,
              server_id: SID, server_name: "Better MC" }];
await window.__bf.loadJobs();
await sleep(300);
check("activity card names the instance",
      jobsHost.textContent.includes("Better MC"));
check("instance chip links to the instance",
      !!$(`#jobList [data-open="${SID}"]`));

check("no uncaught page errors", errors.length === 0, errors.slice(0, 2).join(" | "));
const passed = out.filter(Boolean).length;
console.log(`\n${passed}/${out.length} checks passed`);
process.exit(passed === out.length ? 0 : 1);
