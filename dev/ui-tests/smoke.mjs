// A quick boot check for the new shell: does it render, wire up and route
// without throwing? The full harnesses come once the screens exist.
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
window.fetch = (url, opts) => globalThis.fetch(String(url).startsWith("http") ? url : BASE + url, opts);
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
check("shell renders", !!$(".spine") && !!$(".topbar") && !!$(".canvas"));
check("systems panel populated", $$("#systems .sysrow").length >= 4, $$("#systems .sysrow").length + " rows");
check("systems reports a verdict", /good|sort|broken/.test($("#systemsN").textContent), $("#systemsN").textContent);
check("crafty latency shown", /\d+ ms/.test($("#systems").textContent), ($("#systems").textContent.match(/reachable[^\n]*/)||[""])[0].trim());
check("disk free shown", /GB free/.test($("#systems").textContent));
check("AI row names the real endpoint", !/127\.0\.0\.1:11434|qwen2\.5/.test($("#systems").textContent),
      "the design hard-coded a local ollama and the wrong model");
check("fleet rendered", !!$("#fleet"));
check("discover is the landing view", $('.nav-item[data-view="discover"]').classList.contains("on"));
check("tab strip built for discover", $$("#tabs .tab").length === 3, $$("#tabs .tab").map(t=>t.textContent.trim()).join(", "));

await window.__bf.go("activity");
await sleep(300);
check("activity has no tabs of its own", $$("#tabs .tab").length === 0);
await window.__bf.go("discover", "roulette");
await sleep(300);
check("routing back to discover restores its tabs", $$("#tabs .tab").length === 3);

window.__bf.openPalette();
await sleep(400);
check("command palette opens", !!$(".sheet.pal"));
check("palette lists discover sections", $(".pal-list").textContent.includes("Mod Roulette"));
check("palette offers a way out", !!$("#palEsc"), "a palette with no close affordance is a trap");
$("#palEsc").click();
await sleep(200);
check("palette closes", !$(".sheet.pal"));
window.document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true }));
await sleep(300);
check("ctrl+K reopens it", !!$(".sheet.pal"));
window.document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
await sleep(200);
check("escape closes it", !$(".sheet.pal"));

window.__bf.toast("hello", "ok");
await sleep(100);
check("toasts render", !!$(".toast.ok"));

check("no uncaught page errors", errors.length === 0, errors.slice(0,2).join(" | "));
const passed = out.filter(Boolean).length;
console.log(`\n${passed}/${out.length} checks passed`);
process.exit(passed === out.length ? 0 : 1);
