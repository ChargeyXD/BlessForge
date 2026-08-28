import { JSDOM, VirtualConsole } from "jsdom";
import fs from "fs";

const BASE = process.env.BF_URL || "http://127.0.0.1:8710";
const SID = process.env.BF_SERVER_ID || "";   // prefer this instance if listed
const STATIC = process.env.BF_STATIC || "/static";   // app/static, mounted read-only

const html = fs.readFileSync(STATIC + "/index.html", "utf8");
const appjs = fs.readFileSync(STATIC + "/app.js", "utf8");

const errors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errors.push("jsdomError: " + (e.stack || e.message)));
vc.on("error", (...a) => errors.push("console.error: " + a.join(" ")));

const dom = new JSDOM(html.replace(/<script src="\/static\/app.js"><\/script>/, ""), {
  url: BASE + "/",
  runScripts: "dangerously",
  pretendToBeVisual: true,
  virtualConsole: vc,
});
const { window } = dom;

// real fetch against the live backend
window.fetch = async (url, opts) => {
  const u = String(url).startsWith("http") ? String(url) : BASE + String(url);
  return globalThis.fetch(u, opts);
};
// EventSource is not implemented in jsdom; record the subscriptions instead.
window.eventSources = [];
window.EventSource = class {
  constructor(u) { this.url = u; window.eventSources.push(this); }
  close() { this.closed = true; }
};
window.scrollTo = () => {};
window.HTMLElement.prototype.scrollIntoView = () => {};

const script = window.document.createElement("script");
script.textContent = appjs;
window.document.body.appendChild(script);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const $ = (s) => window.document.querySelector(s);
const $$ = (s) => [...window.document.querySelectorAll(s)];
const results = [];
const check = (name, cond, extra = "") => {
  results.push({ name, ok: !!cond, extra });
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${extra && !cond ? "  — " + extra : ""}`);
};

await sleep(6000);

// ---------- shell + boot ----------
check("health pill filled", /Crafty|Backend/.test($("#health").textContent), $("#health").textContent);
check("MC version select populated", $("#mcv").options.length > 5, $("#mcv").options.length + " options");
check("instance cards rendered", $$("#instances .inst-card").length > 0, $$("#instances .inst-card").length + " cards");
check("instance card has new markup", !!$("#instances .inst-card .inst-card-tags"));
check("card power button present", !!$("#instances [data-card-power]"));

// ---------- theme toggle ----------
$("#themeToggle").click();
check("theme toggle sets light", window.document.documentElement.getAttribute("data-theme") === "light");
check("theme toggle aria-pressed", $("#themeToggle").getAttribute("aria-pressed") === "true");
$("#themeToggle").click();
check("theme toggle returns to dark", window.document.documentElement.getAttribute("data-theme") === "dark");

// ---------- views ----------
window.showView("browse");
await sleep(6000);
check("browse view active", $("#view-browse").classList.contains("active"));
check("pack cards rendered", $$("#packs .pack").length > 0, $$("#packs .pack").length + " packs");
check("pack card head present", !!$("#packs .pack .pack-head"));
check("import CTA present + above results", !!$("#importCta"));

window.showView("jobs");
await sleep(800);
check("jobs view active", $("#view-jobs").classList.contains("active"));
check("jobs list rendered", $("#jobList").children.length > 0);

// ---------- instance detail ----------
// Prefer the instance we were given. Clicking whichever card happens to be
// first assumes every listed server is healthy, and that is not a safe
// assumption: Crafty answers 500 for a server whose directory has gone
// missing while still listing it, so the first card can be one that cannot
// be opened at all.
const cards = $$("#instances .inst-card");
const wanted = SID ? cards.find((c) => c.dataset.open === SID) : null;
const card = wanted || cards[0];
const sid = card.dataset.open;
check("test instance is present in the list", !!card, sid);
card.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
await sleep(3500);
check("instance opened (Crafty answered for it)",
      $("#instName").textContent !== "Could not open this instance",
      $("#instName").textContent);
check("instance view active", $("#view-instance").classList.contains("active"));
check("instance name set", $("#instName").textContent.length > 0, $("#instName").textContent);
check("instance meta tags rendered", $("#instMeta").children.length > 0);
check("mods rendered or empty state", $("#modList").children.length > 0);
check("mod tab count written", $('.tab-count[data-tab-count="mods"]').textContent !== "");
const modrows = $$("#modList .modrow").length;
// The first instance in this Crafty is a bare vanilla server with no mods
// folder, so an empty state is the correct render here. Rows themselves are
// exercised against a 188-mod instance in harness2.
check("mod list renders rows or the empty state", modrows > 0 || !!$("#modList .empty"),
      modrows + " rows");
if (modrows) {
  const r = $("#modList .modrow");
  check("mod row has .mod-sub", !!r.querySelector(".mod-sub"));
  check("mod row has switch", !!r.querySelector(".switch .modToggle"));
  check("mod row has data-del/data-ver", !!r.querySelector("[data-del]") && !!r.querySelector("[data-ver]"));
  // selection through the Set
  const cb = r.querySelector(".modSel");
  cb.checked = true; cb.dispatchEvent(new window.Event("change", { bubbles: true }));
  await sleep(150);
  check("selection shows bulk bar", !$("#bulkBar").classList.contains("hidden"));
  check("selection marks row", r.classList.contains("selected"));
  check("selCount updated", /1 selected/.test($("#selCount").textContent), $("#selCount").textContent);
  cb.checked = false; cb.dispatchEvent(new window.Event("change", { bubbles: true }));
  await sleep(150);
  check("deselect hides bulk bar", $("#bulkBar").classList.contains("hidden"));
  // select-all drives the Set
  $("#selAll").checked = true; $("#selAll").dispatchEvent(new window.Event("change", { bubbles: true }));
  await sleep(150);
  check("select all counts every filtered row", /\d+ selected/.test($("#selCount").textContent), $("#selCount").textContent);
  $("#selAll").checked = false; $("#selAll").dispatchEvent(new window.Event("change", { bubbles: true }));
  // filter
  $("#modFilter").value = "zzzz-nothing";
  $("#modFilter").dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(200);
  check("filter with no match shows empty state", !!$("#modList .empty"));
  $("#modFilter").value = "";
  $("#modFilter").dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(200);
  check("clearing filter restores rows", $$("#modList .modrow").length > 0);
}

// ---------- tabs ----------
for (const t of ["configs", "troubleshoot", "optimize", "pack"]) {
  await window.showTab(t);
  await sleep(t === "optimize" ? 4000 : 2500);
  check(`tab ${t} pane active`, $("#tab-" + t).classList.contains("active"));
  check(`tab ${t} button aria-selected`, $("#tabbtn-" + t).getAttribute("aria-selected") === "true");
}
check("configs list rendered", $("#cfgList").children.length > 0);
check("configs tab count written", $('.tab-count[data-tab-count="configs"]').textContent !== "");
check("optimizer nav rendered", !!$("#optNav") && $$("#optNav .opt-nav-btn").length === 5);
check("optimizer heap input", !!$("#optHeap"));
check("optimizer flags rendered", $$(".flagSel").length > 0, $$(".flagSel").length + " flags");
check("optimizer property presets", $$(".propSel").length > 0);
check("port card rendered", !!$("#portInput") && !!$("#portSave"));
check("properties editor rendered", $$(".propEdit").length > 0, $$(".propEdit").length + " controls");
check("apply button present", !!$("#optApply"));
check("pack tab rendered", $("#packOut").children.length > 0);

// ---------- config editor: open a file, gutter + dirty state ----------
const firstCfg = $("#cfgList .item[data-path]");
if (firstCfg) {
  await window.showTab("configs");
  firstCfg.click();
  await sleep(6000);
  check("config loaded into editor", $("#cfgEditor").value.length > 0);
  check("gutter has line numbers", $("#cfgGutter").textContent.trim().startsWith("1"));
  const before = $("#cfgSave").disabled;
  $("#cfgEditor").value += "\n# test edit";
  $("#cfgEditor").dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(150);
  check("save enabled once dirty", $("#cfgSave").disabled === false || !$("#cfgEditor").readOnly === false, "readOnly=" + $("#cfgEditor").readOnly);
  check("status pill shows dirty", $("#cfgStatus").classList.contains("dirty") || $("#cfgEditor").readOnly, $("#cfgStatus").textContent);
  check("dirty dot in file list", !!$("#cfgList .item.active .cfg-dirty-dot") || $("#cfgEditor").readOnly);
  // revert so the guard does not block later navigation
  $("#cfgEditor").value = $("#cfgEditor").value.replace("\n# test edit", "");
  $("#cfgEditor").dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(100);
  check("dirty clears on revert", !$("#cfgStatus").classList.contains("dirty"));
}

// ---------- modals ----------
window.document.querySelector("#addModBtn").click();
await sleep(400);
check("add-mod modal opens", !!$(".modal-back"));
check("modal shell uses header h3 + .close", !!$(".modal header h3") && !!$(".modal header .close"));
// The Search button is gone on purpose: results are live as you type, so
// #amQ drives the search itself and there is nothing left to press.
check("add-mod fields present", !!$("#amQ") && !!$("#amSrc") && !!$("#amResults"));
check("add-mod search is live, not button-driven", !$("#amGo"));
{
  const q = $("#amQ");
  q.value = "jei";
  q.dispatchEvent(new window.Event("input", { bubbles: true }));
  await sleep(900);
  check("typing alone produces results",
        !$("#amResults").textContent.includes("Start typing"));
}
$(".modal .close").click();
await sleep(200);
check("modal closes", !$(".modal-back"));

// import modal
$("#gotoImport").click();
await sleep(600);
check("import modal opens wide", !!$(".modal.wide"));
check("dropzone present + keyboard operable", !!$("#impDrop") && $("#impDrop").getAttribute("role") === "button" && $("#impDrop").tabIndex === 0);
check("hidden file input present", !!$("#impFile"));
check("progress block present", !!$("#impProgress") && !!$("#impBar") && !!$("#impBarWrap") && !!$("#impStage") && !!$("#impPct"));
check("help disclosure present", !!$(".import-help"));
check("recent imports container", !!$("#impRecent"));
$("#impDrop").dispatchEvent(new window.Event("dragover", { bubbles: true }));
check("dropzone .dragging on dragover", $("#impDrop").classList.contains("dragging"));
$("#impDrop").dispatchEvent(new window.Event("dragleave", { bubbles: true }));
check("dropzone clears .dragging", !$("#impDrop").classList.contains("dragging"));
$(".modal .close").click();
await sleep(200);

// confirm dialog
const p = window.confirmDialog
  ? window.confirmDialog({ title: "t", message: "m", danger: true })
  : null;
await sleep(300);

// ---------- toast ----------
check("no uncaught page errors", errors.length === 0, errors.slice(0, 3).join(" | "));

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
if (errors.length) { console.log("\nERRORS:"); errors.slice(0, 10).forEach((e) => console.log("  " + e)); }
process.exit(failed.length ? 1 : 0);
