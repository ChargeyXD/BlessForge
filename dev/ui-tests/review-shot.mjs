// Drive the client-only review overlay open on a real pack and photograph it.
// Clicks the same route a user takes and STOPS at the review: the overlay is
// shown before anything is written, so nothing is installed.
import puppeteer from "puppeteer";

const BASE = process.env.BF_URL || "http://127.0.0.1:8710";
const OUT = process.env.BF_OUT || "/out";
const PACK = process.env.BF_PACK_NAME || "Better OneBlock";

const browser = await puppeteer.launch({ args: ["--no-sandbox"] });
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 1050 });
page.on("pageerror", (e) => console.log("pageerror:", e.message));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const click = (t) => page.evaluate((t) => {
  const all = [...document.querySelectorAll("button,a,div")].filter(e =>
    e.offsetParent && (e.tagName !== "DIV" || getComputedStyle(e).cursor === "pointer"));
  const el = all.reverse().find(e =>
    (e.innerText || "").replace(/\s+/g, " ").trim().includes(t));
  if (el) { el.click(); return true; }
  return false;
}, t);

await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 60000 });
await sleep(7000);

console.log("discover:", await click("Discover modpacks"));
await sleep(3500);

const box = await page.$("input[placeholder*='modpack' i], input[placeholder*='search' i]");
if (box) { await box.click(); await box.type(PACK); }
await sleep(6000);

console.log("pack card:", await click(PACK));
await sleep(5000);
console.log("install:", await click("Install this pack"));

// Preflight downloads and inspects ~200 jars.
let open = false;
for (let i = 0; i < 60; i++) {
  await sleep(4000);
  open = await page.evaluate(() =>
    !!document.querySelector("[data-screen-label=\'Client-only review\']"));
  if (open) break;
}
console.log("review overlay open:", open);
if (!open) { await page.screenshot({ path: `${OUT}/no-review.png` }); await browser.close(); process.exit(1); }
await sleep(2000);

const shot = `${OUT}/review.png`;
await page.screenshot({ path: shot });
console.log("wrote", shot);

// Measure rather than eyeball: does any child of the card paint over the one
// above it, and is the headline clipped?
const geom = await page.evaluate(() => {
  const panel = document.querySelector("[data-screen-label=\'Client-only review\']");
  const card = panel.firstElementChild;
  const kids = [...card.children].map((el, i) => {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return { i, top: Math.round(r.top), bottom: Math.round(r.bottom),
             h: Math.round(r.height), scrollH: el.scrollHeight,
             flexShrink: cs.flexShrink, overflowY: cs.overflowY,
             clipped: el.scrollHeight > Math.ceil(r.height) + 1,
             text: (el.innerText || "").replace(/\s+/g, " ").slice(0, 44) };
  });
  const overlaps = [];
  for (let i = 1; i < kids.length; i++)
    if (kids[i].top < kids[i - 1].bottom - 1)
      overlaps.push(`child ${i} starts ${kids[i - 1].bottom - kids[i].top}px above child ${i - 1} ends`);
  const h2 = card.querySelector("h2");
  return {
    cardH: Math.round(card.getBoundingClientRect().height),
    viewportH: window.innerHeight, kids, overlaps,
    headline: h2 && { text: (h2.innerText || "").slice(0, 70),
                      h: Math.round(h2.getBoundingClientRect().height),
                      clipped: h2.scrollHeight > h2.clientHeight + 1 },
  };
});
console.log(JSON.stringify(geom, null, 1));
await browser.close();
