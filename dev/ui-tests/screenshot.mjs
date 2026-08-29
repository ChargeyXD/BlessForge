// What it actually looks like. jsdom applies no CSS and ui.mjs asserts rather
// than shows, so this is the only thing that catches "renders, but wrong".
//
// Shoots every screen at 1600px and the shell at three narrower widths, and
// reports what only a rendering engine knows: whether the page overflows
// sideways and whether anything collapsed to nothing.
//
// Read-only, like ui.mjs. It clicks tabs; it never presses a power or delete
// button.
import puppeteer from "puppeteer";

const BASE = process.env.BF_URL || "http://127.0.0.1:8710";
const browser = await puppeteer.launch({ args: ["--no-sandbox"] });
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 1050 });
const msgs = [];
page.on("console", (m) => { if (m.type() === "error") msgs.push(m.text()); });
page.on("pageerror", (e) => msgs.push("pageerror: " + e.message));

const click = (t) => page.evaluate((t) => {
  const all = [...document.querySelectorAll("button,a,div")].filter(e =>
    e.offsetParent && (e.tagName !== "DIV" || getComputedStyle(e).cursor === "pointer"));
  const el = all.reverse().find(e =>
    (e.innerText || "").replace(/\s+/g, " ").trim().includes(t));
  if (el) { el.click(); return true; }
  return false;
}, t);

const probe = (label) => page.evaluate((label) => ({
  label,
  nodes: document.querySelectorAll("body *").length,
  overflows: document.documentElement.scrollWidth > window.innerWidth + 1,
  collapsed: [...document.querySelectorAll("section,aside")]
    .filter(e => e.offsetParent && e.getBoundingClientRect().height < 4).length,
}), label);

await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 60000 });
await new Promise((r) => setTimeout(r, 8000));

const shots = [];
async function shoot(name, wait = 3500) {
  await new Promise((r) => setTimeout(r, wait));
  await page.screenshot({ path: name + ".png" });
  shots.push(await probe(name));
}

// the first server in the spine, then every tab of it
await page.evaluate(() => {
  const c = [...document.querySelectorAll("[data-bf-spine] div")]
    .filter(e => e.offsetParent && getComputedStyle(e).cursor === "pointer")
    .find(e => /RUNNING|STOPPED|FAILED|ORPHAN|DEBRIS/.test(e.innerText || ""));
  if (c) c.click();
});
await shoot("01-situation");
for (const [tab, name, wait] of [
  ["Diagnose", "02-diagnose", 9000], ["Mods", "03-mods", 6000],
  ["Configs", "04-configs", 8000], ["Tune", "05-tune", 7000],
  ["Console", "06-console", 6000], ["Activity", "07-activity", 3000],
]) { await click(tab); await shoot(name, wait); }

await click("Discover modpacks"); await shoot("08-catalogue", 5000);
await click("Mod Roulette");      await shoot("09-roulette", 5000);
await click("Import your own");   await shoot("10-import", 3000);

for (const w of [1280, 900, 560]) {
  await page.setViewport({ width: w, height: 1000 });
  await shoot(`11-width-${w}`, 2000);
}

for (const s of shots) console.log(JSON.stringify(s));
console.log("CONSOLE:", msgs.length ? msgs.slice(0, 8).join("\n  ") : "clean");
await browser.close();
