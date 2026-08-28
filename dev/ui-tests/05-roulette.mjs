// The Mod Roulette screen, driven against the live backend. Rolls are read-only
// until accepted, so this exercises the real pool, a real deal and a real
// reroll without writing anything.
import { JSDOM, VirtualConsole } from "jsdom";
import fs from "fs";
const BASE = process.env.BF_URL || "http://127.0.0.1:8710";
const html = fs.readFileSync("/static/index.html", "utf8");
const appjs = fs.readFileSync("/static/app.js", "utf8");
const errors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errors.push("jsdomError: " + (e.stack || e.message)));
vc.on("error", (...a) => errors.push("console.error: " + a.join(" ")));
const dom = new JSDOM(html.replace(/<script src="\/static\/app.js"><\/script>/, ""),
  { url: BASE + "/", runScripts: "dangerously", pretendToBeVisual: true, virtualConsole: vc });
const { window } = dom;
window.EventSource = class { constructor(u){ this.url=u; } close(){} };
window.CSS = { escape: (s) => String(s).replace(/["\\]/g, "\\$&") };
const writes = [];
const realFetch = globalThis.fetch;
window.fetch = (url, opts = {}) => {
  const p = String(url);
  if ((opts.method || "GET") !== "GET" && /install|switch-pack|action|delete/.test(p)) {
    writes.push(p);
    return Promise.resolve(new globalThis.Response(JSON.stringify({ job_id: "fake" }),
      { status: 200, headers: { "Content-Type": "application/json" } }));
  }
  return realFetch(p.startsWith("http") ? p : BASE + p, opts);
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
const check = (n, c, extra="") => { out.push(!!c); console.log(`${c?"PASS":"FAIL"}  ${n}${extra?"  — "+extra:""}`); };

await sleep(6000);
await window.__bf.go("discover", "roulette");
await sleep(1200);

check("roulette screen renders", !!$("#rSeed") && !!$("#rPull"));
check("a seed is minted on arrival", /^[A-Z2-9]{3}-[A-Z2-9]{3}-[A-Z2-9]{2}$/.test($("#rSeed").value), $("#rSeed").value);
check("seed avoids ambiguous glyphs", !/[ILO01]/.test($("#rSeed").value.replace(/-/g,"")));
check("three segmented controls", $$("#rSegs .seg").length === 3);
check("three sliders", $$("#rSliders .range").length === 3);
check("nine categories", $$("#rCats .cat").length === 9, $$("#rCats .cat").length + " shown");
check("four toggles", $$("#rToggles .tog").length === 4);
check("empty state before a pull", $("#rOut").textContent.includes("Nothing rolled yet"));

// the pool counter is debounced + job-backed; give it room
await sleep(9000);
check("pool counter resolves", /\d/.test($(".pool-n")?.textContent || ""), ($(".pool-n")?.textContent || "").trim());
check("pool breaks down by category", $$(".pool-bar").length === 9);

// cycle a category through neutral -> preferred -> banned
const cat = $$("#rCats .cat")[0];
const key = cat.dataset.cat;
cat.click(); await sleep(150);
check("a category cycles to preferred", window.__bf.state && $(`[data-cat="${key}"]`).classList.contains("s1"));
$(`[data-cat="${key}"]`).click(); await sleep(150);
check("...then to banned", $(`[data-cat="${key}"]`).classList.contains("s2"));
$(`[data-cat="${key}"]`).click(); await sleep(150);
check("...then back to neutral", $(`[data-cat="${key}"]`).classList.contains("s0"));

// a real roll
$("#rPull").click();
for (let i = 0; i < 60 && !$(".hand-row"); i++) await sleep(500);
check("pulling deals a hand", $$(".hand-row").length > 0, $$(".hand-row").length + " mods");
check("the odds panel appears", !!$(".odds"), ($(".odds h3")?.textContent || "").trim());
check("odds give reasons, not just a verdict", $$(".odds .why li").length >= 3);
check("four headline stats", $$(".odds .stat").length === 4,
      $$(".odds .stat .k").map(k=>k.textContent).join(", "));
check("the hand names real mods", ($(".hand-row .n")?.textContent || "").length > 2,
      $(".hand-row .n")?.textContent);
check("rows carry category and size", /·/.test($(".hand-row .meta")?.textContent || ""),
      $(".hand-row .meta")?.textContent);

const seedAfter = $("#rSeed").value;
check("the seed is shown with the result", $(".rollbar").textContent.includes(seedAfter));

// hold a row
const first = $(".hand-row .n").textContent;
$("[data-hold]").click(); await sleep(200);
check("holding a row marks it", !!$(".hand-row.held") && !!$(".hold.on"));
check("held row is the one clicked", $(".hand-row.held .n").textContent === first);

// reroll a single slot
const before = $$(".hand-row .n").map(n => n.textContent);
$$("[data-reroll]")[1].click();
for (let i = 0; i < 30; i++) { await sleep(400); if ($$(".hand-row .n").map(n=>n.textContent).join()!==before.join()) break; }
const after = $$(".hand-row .n").map(n => n.textContent);
const diff = before.filter((n,i) => n !== after[i]).length;
check("rerolling one slot changes exactly one row", diff === 1, `${diff} rows changed`);

// accept opens a dialog and writes nothing until confirmed
$("#rAccept").click(); await sleep(400);
check("accepting asks for a name and port", !!$("#rName") && !!$("#rPort"));
check("nothing was written by opening it", writes.length === 0);
$$(".sheet .btn").find(b => /Back/.test(b.textContent)).click(); await sleep(200);
check("backing out writes nothing", writes.length === 0 && !$("#rName"));

check("no uncaught page errors", errors.length === 0, errors.slice(0,2).join(" | "));
const passed = out.filter(Boolean).length;
console.log(`\n${passed}/${out.length} checks passed`);
process.exit(passed === out.length ? 0 : 1);
