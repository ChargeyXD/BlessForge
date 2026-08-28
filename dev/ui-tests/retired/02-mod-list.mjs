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

// Writes are stubbed: this drives a live server and must not mutate it.
const writes = [];
window.fetch = async (url, opts = {}) => {
  const path = String(url);
  const method = (opts.method || "GET").toUpperCase();
  if (method !== "GET") {
    writes.push(method + " " + path + " " + (opts.body || ""));
    const body = opts.body ? JSON.parse(opts.body) : {};
    if (path.includes("/mods/toggle")) {
      const file = body.enabled ? body.file.replace(/\.disabled$/, "") : body.file + ".disabled";
      return new window.Response(JSON.stringify({ file, enabled: body.enabled }),
        { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (path.includes("/mods/delete")) {
      return new window.Response(JSON.stringify({ deleted: body.files }),
        { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return new window.Response(JSON.stringify({ ok: true }), { status: 200 });
  }
  return globalThis.fetch(path.startsWith("http") ? path : BASE + path, opts);
};
window.Response = window.Response || globalThis.Response;
window.EventSource = class { constructor(u) { this.url = u; } close() {} };
window.scrollTo = () => {};
window.HTMLElement.prototype.scrollIntoView = () => {};
// jsdom reports every element as zero-height; give the list a viewport so the
// virtualiser has a window to compute.
Object.defineProperty(window.HTMLElement.prototype, "clientHeight", { get() { return 600; }, configurable: true });

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
await sleep(3000);

const rows = $$("#modList .modrow").length;
const spacers = $$("#modList .virt-space").length;
check("mod rows rendered", rows > 0, rows + " mounted");
check("list is virtualised above the threshold", spacers === 2, spacers + " spacers");
check("window is smaller than the full list", rows < 188, rows + " of 188 mounted");
check("mod count pill reads the whole list", /188 mods/.test($("#modStats").textContent), $("#modStats").textContent);
check("tab count reads 188", $('.tab-count[data-tab-count="mods"]').textContent === "188");

// scroll the virtual window
const list = $("#modList");
list.scrollTop = 4000;
list.dispatchEvent(new window.Event("scroll"));
await sleep(200);
const firstAfter = $("#modList .modrow").dataset.file;
check("scrolling repaints a different window", firstAfter !== undefined);
const topSpacer = $$("#modList .virt-space")[0];
check("top spacer grows with scroll", parseInt(topSpacer.style.height) > 0, topSpacer.style.height);
list.scrollTop = 0;
list.dispatchEvent(new window.Event("scroll"));
await sleep(200);

// selection survives a repaint (it lives in a Set, not the DOM)
const target = $("#modList .modrow");
const file = target.dataset.file;
const cb = target.querySelector(".modSel");
cb.checked = true; cb.dispatchEvent(new window.Event("change", { bubbles: true }));
await sleep(120);
check("selection registered", /1 selected/.test($("#selCount").textContent));
list.scrollTop = 5000; list.dispatchEvent(new window.Event("scroll")); await sleep(200);
list.scrollTop = 0; list.dispatchEvent(new window.Event("scroll")); await sleep(200);
const again = $(`#modList .modrow[data-file="${file}"] .modSel`);
check("selection survives unmount + remount", again && again.checked, "still checked after scrolling away and back");
check("count unchanged after repaint", /1 selected/.test($("#selCount").textContent), $("#selCount").textContent);
again.checked = false; again.dispatchEvent(new window.Event("change", { bubbles: true }));
await sleep(120);

// in-place toggle (stubbed write): the row is patched, not the list repainted
const row = $("#modList .modrow");
const before = row.dataset.file;
const scrollBefore = list.scrollTop;
const toggle = row.querySelector(".modToggle");
toggle.checked = false;
toggle.dispatchEvent(new window.Event("change", { bubbles: true }));
await sleep(400);
const patched = $(`#modList .modrow[data-file="${before}.disabled"]`);
check("row patched in place to .disabled", !!patched, patched ? patched.dataset.file : "not found");
check("row marked off", patched && patched.classList.contains("off"));
check("mod-sub filename updated", patched && patched.querySelector(".mod-sub").textContent.startsWith(before + ".disabled"));
check("data-del follows the rename", patched && patched.querySelector("[data-del]").dataset.del === before + ".disabled");
check("stats recount after toggle", /173 enabled/.test($("#modStats").textContent), $("#modStats").textContent);
check("scroll position kept", list.scrollTop === scrollBefore);

// the same toggle under an 'enabled only' filter must mark the row stale, not remove it
$("#modState").value = "enabled";
$("#modState").dispatchEvent(new window.Event("change", { bubbles: true }));
await sleep(300);
const r2 = $("#modList .modrow");
const f2 = r2.dataset.file;
const t2 = r2.querySelector(".modToggle");
t2.checked = false; t2.dispatchEvent(new window.Event("change", { bubbles: true }));
await sleep(400);
const stale = $(`#modList .modrow[data-file="${f2}.disabled"]`);
check("row that leaves the filter stays visible", !!stale);
check("and is marked .stale", stale && stale.classList.contains("stale"));
$("#modState").value = "all";
$("#modState").dispatchEvent(new window.Event("change", { bubbles: true }));
await sleep(300);

check("only expected writes were attempted", writes.every((w) => /mods\/toggle|mods\/icons/.test(w)), writes.join(" ; ").slice(0,200));
check("no uncaught page errors", errors.length === 0, errors.slice(0, 2).join(" | "));

const fails = out.filter((x) => !x).length;
console.log(`\n${out.length - fails}/${out.length} checks passed`);
if (errors.length) errors.slice(0, 6).forEach((e) => console.log("  " + e));
process.exit(fails ? 1 : 0);
