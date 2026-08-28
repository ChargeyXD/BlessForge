import { JSDOM, VirtualConsole } from "jsdom";
import fs from "fs";

const STATIC = process.env.BF_STATIC || "/static";
const BASE = process.env.BF_URL || "http://127.0.0.1:8710";
const SID = process.env.BF_SERVER_ID;   // a Crafty instance with mods

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

// Everything about this job is fake: nothing is installed on the live server.
const sources = [];
window.EventSource = class {
  constructor(u) { this.url = u; this.onmessage = null; sources.push(this); }
  emit(o) { if (this.onmessage) this.onmessage({ data: JSON.stringify(o) }); }
  close() { this.closed = true; }
};
let fakeJob = null;
const json = (o) => new globalThis.Response(JSON.stringify(o),
  { status: 200, headers: { "Content-Type": "application/json" } });
window.fetch = async (url, opts = {}) => {
  const path = String(url);
  const method = (opts.method || "GET").toUpperCase();
  if (method === "GET" && path.startsWith("/api/jobs") && !path.includes("/events")) {
    return json({ items: fakeJob ? [fakeJob] : [] });
  }
  if (method !== "GET") {
    if (path.includes("/mods/identify")) return json({ job_id: "job-test-1" });
    if (path.includes("/cancel")) return json({ cancelled: true });
    return json({ ok: true });
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
await sleep(6000);

$("#identifyMods").click();
await sleep(500);
check("job modal opened", !!$(".modal-back"));
check("phase strip rendered", $$(".job-phases .job-phase").length === 5);
check("bar is a progressbar", $("#jBarWrap").getAttribute("role") === "progressbar");
check("bar starts indeterminate", $("#jBarWrap").classList.contains("indeterminate"));
const es = sources[sources.length - 1];
check("stream opened for the job", /jobs\/job-test-1\/events/.test(es.url));
check("footer offers Cancel and Background",
  $$(".modal .foot .btn").map((b) => b.textContent).join("|").includes("Cancel Task"));

es.emit({ event: "step", step: "Resolving 302 manifest entries", percent: 4, status: "running" });
await sleep(120);
check("phase 1 current", $$(".job-phase")[0].classList.contains("current"));
check("phase label 1 of 5", /Phase 1 of 5/.test($("#jPhaseLabel").textContent));
check("bar determinate once percent arrives", !$("#jBarWrap").classList.contains("indeterminate"));
check("aria-valuenow tracks percent", $("#jBarWrap").getAttribute("aria-valuenow") === "4");

es.emit({ event: "step", step: "Downloading mods — 184 of 302", percent: 61, status: "running" });
await sleep(120);
check("phase advances to Download", $$(".job-phase")[1].classList.contains("current"));
check("earlier phase marked done", $$(".job-phase")[0].classList.contains("done"));

["Resolved 302 of 302 manifest entries|info",
 "The pack wants 8.3 GB but this host can only safely give 3.5 GB|warn",
 "Skipped 1 client-only mod: sound-physics.jar|warn",
 "403 from CurseForge for prominence-core.jar|error",
 "Downloading create.jar|info", "Downloading create.jar|info", "Downloading create.jar|info",
].forEach((l) => {
  const [message, level] = l.split("|");
  es.emit({ event: "log", message, level });
});
await sleep(200);
check("one element per distinct log line", $$("#jLog .log-line").length === 5, $$("#jLog .log-line").length + " lines");
check("warn lines separable", $$("#jLog .log-line.warn").length === 2);
check("error line separable", $$("#jLog .log-line.err").length === 1);
check("duplicates carry ×N", $("#jLog .dupe") && $("#jLog .dupe").textContent === "×3");
check("warning pill counts warn+error", /3 warnings/.test($("#jWarnPill").textContent), $("#jWarnPill").textContent);

es.emit({ event: "stream", message: "The crash points at Create rather than the world data.", total: 55 });
await sleep(150);
check("stream pane revealed", !$("#jStreamWrap").classList.contains("hidden"));
check("stream text written", /crash points at Create/.test($("#jStream").textContent));
check("stream counter written", /55 chars/.test($("#jStreamMeta").textContent));

// ---- Run in Background, then re-attach with Watch ----
$$(".modal .foot .btn").find((b) => /Run in Background/.test(b.textContent)).click();
await sleep(200);
check("background closes the view", !$(".modal-back"));
check("stream keeps running", !es.closed);
es.emit({ event: "log", message: "Registered server in Crafty", level: "info" });
await sleep(100);

fakeJob = { id: "job-test-1", title: "Identifying Mods", status: "running",
            step: "Registering server", percent: 74, error: null };
window.showView("jobs");
await sleep(700);
check("activity card rendered", !!$("#jobList .job-card"));
check("Watch button offered on an active job", !!$("#jobList [data-watch]"));
check("Cancel button offered on an active job", !!$("#jobList [data-cancel]"));
$("#jobList [data-watch]").click();
await sleep(300);
check("Watch re-opens the job view", !!$(".modal-back"));
check("buffered log replayed into the reopened view", $$("#jLog .log-line").length === 6,
  $$("#jLog .log-line").length + " lines replayed");
check("buffered stream replayed", /crash points at Create/.test($("#jStream").textContent));

// ---- completion summary ----
es.emit({ event: "end", status: "done", percent: 100, result: {
  pack: "All of Create", mods_installed: 301, files_uploaded: 618,
  loader: "neoforge", minecraft: "1.21.1",
  problems: [{ name: "Sound Physics Remastered", reason: "403 from CurseForge — author blocks downloads" }],
} });
await sleep(400);
check("modal stays open on done", !!$(".modal-back"));
check("summary hero rendered", !!$(".summary-hero"));
check("hero warns because the log had warnings", $(".summary-hero").classList.contains("warn"));
check("stat grid rendered", !!$(".modal .statgrid"));
check("warnings outlive the modal's progress view", $$(".summary-note").length >= 3,
  $$(".summary-note").length + " notes");
check("failed mods listed from result.problems", $$(".summary-fail").length === 1);
check("failed mod offers Add manually", !!$(".summary-fail [data-add]"));
check("footer swaps to a dismiss action",
  $$(".modal .foot .btn").map((b) => b.textContent).join("|") === "Done", 
  $$(".modal .foot .btn").map((b) => b.textContent).join("|"));
check("stream closed at end", es.closed);

$$(".modal .foot .btn")[0].click();
await sleep(200);
check("summary dismisses cleanly", !$(".modal-back"));
check("no uncaught page errors", errors.length === 0, errors.slice(0, 2).join(" | "));

const fails = out.filter((x) => !x).length;
console.log(`\n${out.length - fails}/${out.length} checks passed`);
if (errors.length) errors.slice(0, 6).forEach((e) => console.log("  " + e));
process.exit(fails ? 1 : 0);
