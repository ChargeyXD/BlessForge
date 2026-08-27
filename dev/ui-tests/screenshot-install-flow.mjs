import puppeteer from "puppeteer";
import fs from "fs";
const BASE = process.env.BF_URL || "http://127.0.0.1:8710";
const result = JSON.parse(fs.readFileSync("preflight-result.json", "utf8"));
const browser = await puppeteer.launch({ args: ["--no-sandbox"] });
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 900 });
const msgs = [];
page.on("console", (m) => msgs.push(`[${m.type()}] ${m.text()}`));
page.on("pageerror", (e) => msgs.push(`[pageerror] ${e.message}\n${e.stack || ""}`));

// Fake the write + the job stream so no real work is started on Crafty.
await page.evaluateOnNewDocument((resultJson) => {
  window.__result = JSON.parse(resultJson);
  const realFetch = window.fetch;
  window.fetch = (url, opts = {}) => {
    const p = String(url);
    if ((opts.method || "GET").toUpperCase() !== "GET" && p.includes("/install/preflight")) {
      return Promise.resolve(new Response(JSON.stringify({ job_id: "pf-1" }),
        { status: 200, headers: { "Content-Type": "application/json" } }));
    }
    return realFetch(url, opts);
  };
  class FakeES {
    constructor(u) { this.url = u; window.__es = this; }
    emit(o) { this.onmessage && this.onmessage({ data: JSON.stringify(o) }); }
    close() { this.closed = true; }
  }
  window.EventSource = FakeES;
}, JSON.stringify(result));

await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 60000 });
await new Promise(r => setTimeout(r, 3000));

const step = async (name, fn, wait = 2500) => {
  await fn();
  await new Promise(r => setTimeout(r, wait));
  await page.screenshot({ path: name });
  console.log("shot:", name);
};

await step("01-instances.png", async () => {});
await step("02-browse.png", async () => { await page.evaluate(() => window.showView("browse")); }, 6000);
await step("03-pack-detail.png", async () => {
  await page.evaluate(() => document.querySelector("#packs .pack").click());
}, 5000);
await step("04-install-wizard.png", async () => {
  await page.evaluate(() => document.querySelector("#verList [data-i]").click());
}, 1500);
await step("05-job-modal.png", async () => {
  await page.evaluate(() => [...document.querySelectorAll(".modal .foot .btn")]
    .find(b => /Continue/.test(b.textContent)).click());
}, 1500);
await step("06-review.png", async () => {
  await page.evaluate(() => {
    window.__es.emit({ event: "step", step: "Analysing pack", percent: 60, status: "running" });
    window.__es.emit({ event: "end", status: "done", percent: 100, result: window.__result });
  });
}, 2000);

const state = await page.evaluate(() => ({
  modalTitle: document.querySelector(".modal header h3")?.textContent,
  reviewRows: document.querySelectorAll(".reviewrow").length,
  footButtons: [...document.querySelectorAll(".modal .foot .btn")].map(b => b.textContent.trim()),
  modalVisible: (() => { const m = document.querySelector(".modal"); if (!m) return "no modal";
    const r = m.getBoundingClientRect(); const cs = getComputedStyle(m);
    return `${Math.round(r.width)}x${Math.round(r.height)} at ${Math.round(r.x)},${Math.round(r.y)} display=${cs.display} opacity=${cs.opacity} vis=${cs.visibility}`; })(),
}));
console.log(JSON.stringify(state, null, 1));
console.log("CONSOLE:", msgs.length ? msgs.slice(0, 15).join("\n  ") : "clean");
await browser.close();
