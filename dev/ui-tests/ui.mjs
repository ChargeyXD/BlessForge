// The front end, in a real browser, against the real backend.
//
// The jsdom harnesses that used to live here are gone: the UI is the Claude
// Design canvas driven by its own React runtime, so a DOM-only fake cannot
// render it and cannot see a layout break either. This drives headless Chrome
// and asserts on what is actually painted.
//
// It only READS. Nothing here starts, stops, installs or deletes anything --
// the destructive paths are exercised by hand, not by a suite that might run
// unattended against someone's fleet.
import puppeteer from "puppeteer";

const BASE = process.env.BF_URL || "http://127.0.0.1:8710";
const out = [];
const check = (n, c, extra = "") => {
  out.push(!!c);
  console.log(`${c ? "PASS" : "FAIL"}  ${n}${extra ? "  — " + extra : ""}`);
};

const browser = await puppeteer.launch({ args: ["--no-sandbox"] });
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 1050 });
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
page.on("requestfailed", (r) => {
  const u = r.url();
  if (!/favicon/.test(u)) errors.push(`reqfail ${u.slice(0, 70)} ${r.failure()?.errorText}`);
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = () => page.evaluate(() => document.body.innerText.replace(/\s+/g, " "));
const count = (sel) => page.evaluate((s) => document.querySelectorAll(s).length, sel);
const overflows = () => page.evaluate(() =>
  document.documentElement.scrollWidth > window.innerWidth + 1);
// The canvas puts a glyph span before most labels and renders the fleet cards
// as clickable divs, so "the button whose text starts with X" finds neither.
// The canvas puts a glyph span before most labels and renders the fleet cards
// as clickable divs, so "the button whose text starts with X" finds neither.
// The cursor is read from computed style: the browser rewrites the inline
// "cursor:pointer" into "cursor: pointer", which an attribute selector misses.
const click = (t) => page.evaluate((t) => {
  const all = [...document.querySelectorAll("button,a,div")].filter(e =>
    e.offsetParent && (e.tagName !== "DIV" || getComputedStyle(e).cursor === "pointer"));
  const el = all.reverse().find(e =>
    (e.innerText || "").replace(/\s+/g, " ").trim().includes(t));
  if (el) { el.click(); return true; }
  return false;
}, t);

await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 60000 });
await sleep(8000);

/* --- the shell ----------------------------------------------------------- */
check("the design canvas boots under its own runtime",
      await page.evaluate(() => !!window.__dcRegistry && !!window.React));
check("React came from this box, not a CDN",
      await page.evaluate(() => [...document.scripts].every(s => !/unpkg|cdn/.test(s.src))),
      "a LAN install has no route to unpkg");
check("the fonts came from this box too",
      await page.evaluate(() => [...document.querySelectorAll('link[rel=stylesheet]')]
        .every(l => !/fonts\.googleapis/.test(l.href))));
check("something actually rendered", (await count("body *")) > 200,
      (await count("body *")) + " nodes");
check("no horizontal overflow at 1600px", !(await overflows()));

const t0 = await text();
check("the systems panel reports Crafty's real latency", /reachable · \d+ server/.test(t0)
      && /\d+ ms/.test(t0));
check("it names the real AI endpoint, not the design's placeholder",
      !/qwen2\.5|127\.0\.0\.1:11434/.test(t0),
      "the canvas hard-coded a local ollama and the wrong model");
check("free disk is measured", /GB free/.test(t0));

/* --- the fleet ----------------------------------------------------------- */
const fleet = await page.evaluate(() =>
  [...document.querySelectorAll("[data-bf-spine] div")]
    .filter(e => e.offsetParent && getComputedStyle(e).cursor === "pointer"
                 && /RUNNING|STOPPED|FAILED|ORPHAN|DEBRIS/.test(e.innerText || ""))
    .map(c => (c.innerText || "").trim().split("\n")[0]));
check("the fleet spine lists servers", fleet.length >= 1, fleet.join(", "));
check("loader icons resolve from /assets",
      await page.evaluate(() => [...document.querySelectorAll('img[src*="assets/"]')]
        .every(i => i.naturalWidth > 0)));

/* --- an instance, tab by tab --------------------------------------------- */
const opened = await page.evaluate((name) => {
  const c = [...document.querySelectorAll("[data-bf-spine] div")]
    .filter(e => e.offsetParent && getComputedStyle(e).cursor === "pointer")
    .find(e => (e.innerText || "").includes(name));
  if (c) { c.click(); return true; } return false;
}, fleet[0]);
check("a server opens", opened);
await sleep(4500);
const sit = await text();
check("Situation states what the server is doing",
      /(RUNNING|STOPPED|FAILED|ORPHAN|DEBRIS)/.test(sit));
check("Situation shows the real data directory, not /srv/minecraft",
      /\/crafty\/servers\//.test(sit) && !/\/srv\/minecraft/.test(sit),
      "the canvas hard-coded /srv/minecraft/<id>");
check("no TPS tile — nothing measures TPS", !/\bTPS\b/.test(sit));
check("the memory tile is not labelled Heap",
      !/HEAP/i.test(sit.slice(0, 600)) || /MEMORY/i.test(sit),
      "Crafty measures the process, not the JVM heap");

for (const [tab, assert_, label] of [
  ["Mods", (t) => /\d+ TOTAL · \d+ ENABLED/.test(t), "mod counts are real"],
  ["Configs", (t) => /\d+ of \d+ shown/.test(t), "config files are listed"],
  // Case-insensitive: the heap card's unit label is styled
  // text-transform:uppercase, and innerText returns the *rendered* text, so a
  // case-sensitive match here tests the styling rather than the readout.
  ["Tune", (t) => /GB heap/i.test(t) && /threads/.test(t), "heap and host facts"],
  ["Activity", (t) => /job/i.test(t), "the job registry"],
]) {
  await click(tab);
  await sleep(tab === "Configs" ? 8000 : 6000);
  const t = await text();
  check(`${tab}: ${label}`, assert_(t), t.slice(t.indexOf(tab), t.indexOf(tab) + 90));
  check(`${tab} does not overflow`, !(await overflows()));
}

/* --- tune, all four sub-tabs --------------------------------------------- */
await click("Tune"); await sleep(5000);
await click("JVM flags"); await sleep(1800);
check("every JVM flag is listed, not a sample",
      /\d+ flags · \d+ on · every one is listed/.test(await text()),
      "the canvas said '8 of 34 flags shown'");
await click("server.properties"); await sleep(1800);
const props = await text();
// Ask the backend which groups this server's file actually has rather than
// hard-coding six: a server whose properties file is still Crafty's stub
// legitimately spans fewer, and the screen should match the data.
const propGroups = await page.evaluate(async (u, name) => {
  const list = await (await fetch(`${u}/api/instances`)).json();
  const srv = (list.items || []).find(i => name.includes(i.name)) || (list.items || [])[0];
  if (!srv) return [];
  const p = await (await fetch(`${u}/api/instances/${srv.server_id}/properties`)).json();
  return (p.groups || []).map(g => g.name);
}, BASE, fleet[0]);
check("server.properties offers every group, not a sample",
      propGroups.length > 0 && propGroups.every(g => props.includes(g)),
      propGroups.join(", "));
check("and says how many keys there are", /\d+ of \d+ keys shown/.test(props),
      (props.match(/\d+ of \d+ keys shown/) || [""])[0]);
await click("Ports"); await sleep(1800);
check("the ports card names Crafty's real published range",
      /25500–25600/.test(await text()),
      "the canvas said 25565-25575");

/* --- discover ------------------------------------------------------------ */
await click("Discover modpacks"); await sleep(4000);
const cat = await text();
check("the catalogue lists real packs", /\d+ of [\d,]+ packs/.test(cat), cat.slice(0, 60));
check("packs show their real logos",
      (await page.evaluate(() => [...document.querySelectorAll('img[src*="forgecdn"]')].length)) > 3);

await click("Mod Roulette"); await sleep(4000);
const rou = await text();
check("Mod Roulette has its seed", /[A-Z0-9]{3}-[A-Z0-9]{3}-[A-Z0-9]{2}/.test(rou));
check("the Minecraft version is a full list, not three fixed options",
      await page.evaluate(() => {
        const s = [...document.querySelectorAll("select")]
          .find(x => x.previousElementSibling &&
                     /MINECRAFT VERSION/.test(x.previousElementSibling.textContent || ""));
        return !!s && s.options.length > 5;
      }),
      "the canvas offered exactly 1.20.1 / 1.21.1 / 1.21.4");
check("the pool counter is a real number", /\d+/.test(rou) && !/1170 of 30\b/.test(rou));
check("the reels and lever are present", /PULL/.test(rou));

await click("Import your own"); await sleep(3000);
check("the import screen renders", /drop|zip|import/i.test(await text()));

/* --- palette ------------------------------------------------------------- */
await page.keyboard.down("Control"); await page.keyboard.press("k"); await page.keyboard.up("Control");
await sleep(1200);
check("the command palette opens", /GO TO|SERVERS/.test(await text()));
await page.keyboard.press("Escape"); await sleep(800);
check("escape closes it", !/GO TO/.test(await text()));

/* --- responsive ---------------------------------------------------------- */
for (const w of [1280, 900]) {
  await page.setViewport({ width: w, height: 900 });
  await sleep(1200);
  check(`no horizontal overflow at ${w}px`, !(await overflows()));
}

check("no uncaught page errors", errors.length === 0, errors.slice(0, 3).join(" | "));

const passed = out.filter(Boolean).length;
console.log(`\n${passed}/${out.length} checks passed`);
await browser.close();
process.exit(passed === out.length ? 0 : 1);
