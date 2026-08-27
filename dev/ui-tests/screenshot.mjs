import puppeteer from "puppeteer";
const BASE = process.env.BF_URL || "http://127.0.0.1:8710";
const browser = await puppeteer.launch({ args: ["--no-sandbox"] });
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 900 });
const msgs = [];
page.on("console", (m) => msgs.push(`[${m.type()}] ${m.text()}`));
page.on("pageerror", (e) => msgs.push(`[pageerror] ${e.message}`));
page.on("requestfailed", (r) => msgs.push(`[reqfail] ${r.url()} ${r.failure()?.errorText}`));
await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 60000 });
await new Promise(r => setTimeout(r, 4000));
await page.screenshot({ path: "01-instances.png" });
const info = await page.evaluate(() => ({
  cards: document.querySelectorAll("#instances .inst-card").length,
  health: document.querySelector("#health")?.textContent.trim(),
  styleApplied: getComputedStyle(document.body).backgroundColor,
  headerH: document.querySelector(".app-header")?.getBoundingClientRect().height,
  cardH: document.querySelector(".inst-card")?.getBoundingClientRect().height,
  bodyScrollW: document.body.scrollWidth,
  innerW: window.innerWidth,
}));
console.log(JSON.stringify(info, null, 1));
console.log("CONSOLE:", msgs.length ? msgs.slice(0, 20).join("\n  ") : "clean");
await browser.close();
