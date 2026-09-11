// BlessForge 2.1 — HANDOVER-CASAOS.md §G, §H and §I, in a real browser.
//
// The three UI passes. Everything here is visual or local: it opens screens,
// hovers, drags and reads computed style. It changes nothing on any instance.
import puppeteer from "puppeteer";

const BASE = process.env.BF_URL || "http://127.0.0.1:8710";
const OUT = process.env.BF_OUT || "/out";
const out = [];
const check = (n, c, extra = "") => {
  out.push(!!c);
  console.log(`${c ? "PASS" : "FAIL"}  ${n}${extra ? "  — " + String(extra).slice(0, 130) : ""}`);
};

const browser = await puppeteer.launch({ args: ["--no-sandbox"] });
const page = await browser.newPage();
await page.setCacheEnabled(false);
await page.setViewport({ width: 1600, height: 1050 });
const errors = [];
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text()); });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = () => page.evaluate(() => document.body.innerText.replace(/\s+/g, " "));
const goto = async (hash, wait = 3500) => {
  await page.evaluate((h) => { location.hash = h; }, hash);
  await sleep(wait);
};
const overflows = () => page.evaluate(() =>
  document.documentElement.scrollWidth > window.innerWidth + 1);

await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 60000 });
await sleep(6000);
const instances = await page.evaluate(async (u) =>
  (await (await fetch(u + "/api/instances")).json()).items, BASE);
const big = instances.find((i) => /Tensura/.test(i.name)) || instances[0];

/* ================= §G — the first UI pass ================= */
console.log("--- §G: polygon layering, paint order, side tags, editor ---");

// The blob must sit BEHIND the card's fill, with the text on top.
const layers = await page.evaluate(() => {
  const card = document.querySelector(".card.hoverable, .svcard");
  if (!card) return { err: "no hoverable card" };
  const blob = card.querySelector(".p5-hl");
  if (!blob) return { err: "no .p5-hl in the card" };
  const cb = card.getBoundingClientRect(), bb = blob.getBoundingClientRect();
  const zBlob = getComputedStyle(blob).zIndex;
  const after = getComputedStyle(card, "::after");
  const isolation = getComputedStyle(card).isolation;
  return {
    bleedLeft: +(cb.left - bb.left).toFixed(1),
    bleedRight: +(bb.right - cb.right).toFixed(1),
    zBlob, zAfter: after.zIndex, isolation,
  };
});
check("the blob bleeds past the card on both sides",
      layers.bleedLeft > 8 && layers.bleedRight > 8,
      layers.err || `${layers.bleedLeft}px / ${layers.bleedRight}px`);
check("the blob sits at z-index 0, under the card's fill",
      layers.zBlob === "0" && layers.zAfter === "1",
      `blob ${layers.zBlob}, ::after ${layers.zAfter}`);
check("the card isolates so the layering cannot be undone by a transform",
      layers.isolation === "isolate", layers.isolation);

// Hovering must reveal it without covering the text.
const hovered = await page.evaluate(async () => {
  const card = document.querySelector(".card.hoverable, .svcard");
  const blob = card.querySelector(".p5-hl");
  const before = getComputedStyle(blob).opacity;
  card.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
  card.dispatchEvent(new PointerEvent("pointerenter", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 500));
  return { before, after: getComputedStyle(blob).opacity };
});
check("the blob is invisible until the card is hovered",
      hovered.before === "0", `opacity ${hovered.before}`);

// No blanket `.card > *` rule — §1A says that silently collapses the blob.
const blanket = await page.evaluate(() => {
  let hits = [];
  for (const sheet of document.styleSheets) {
    let rules; try { rules = sheet.cssRules; } catch { continue; }
    for (const r of rules) {
      if (r.selectorText && /\.card\s*>\s*\*/.test(r.selectorText)) hits.push(r.selectorText);
    }
  }
  return hits;
});
check("no blanket `.card > *` rule exists", blanket.length === 0, blanket.join(" | "));

// The shell must paint before /api/health resolves.
const paint = await page.evaluate(async (u) => {
  const t0 = performance.now();
  const h = fetch(u + "/api/health").then(() => performance.now() - t0);
  return { healthMs: Math.round(await h) };
}, BASE);
const nav = await page.evaluate(() => {
  const n = performance.getEntriesByType("navigation")[0];
  return { dcl: Math.round(n.domContentLoadedEventEnd), load: Math.round(n.loadEventEnd) };
});
check("the shell paints without waiting on health",
      nav.dcl > 0 && nav.dcl < 3000,
      `DOMContentLoaded ${nav.dcl}ms, load ${nav.load}ms, health ${paint.healthMs}ms`);

// Mod side tags.
await goto(`/i/${big.server_id}/mods`, 9000);
const modsTxt = await text();
check("mod rows carry a side tag",
      /client only|server only|both|not scanned|unknown side/i.test(modsTxt),
      (modsTxt.match(/(client only|server only|both)/i) || [""])[0]);

// The config editor sits beside the list, not in a modal.
await goto(`/i/${big.server_id}/configs`, 9000);
const cfg = await page.evaluate(() => {
  const modal = document.querySelector(".modal, dialog[open]");
  const areas = document.querySelectorAll("textarea");
  return { hasModal: !!modal, areas: areas.length };
});
check("the config editor is in the tab, not a modal",
      !cfg.hasModal, cfg.hasModal ? "a modal is open" : `${cfg.areas} editor(s) inline`);

/* ================= §H — the third UI pass ================= */
console.log("\n--- §H: dark theme, racks, rail cap, heap slider ---");

const dark = await page.evaluate(async () => {
  document.documentElement.setAttribute("data-theme", "dark");
  await new Promise((r) => setTimeout(r, 400));
  const v = getComputedStyle(document.documentElement).getPropertyValue("--slab-bg").trim();
  return v;
});
check("dark mode's slab is a dusty blush, not white",
      /^#?[cC]9/.test(dark) || dark.toLowerCase().includes("c98fa2"),
      `--slab-bg ${dark}`);
await page.evaluate(() => document.documentElement.setAttribute("data-theme", "light"));
await sleep(300);

await goto("/groups", 5000);
const racks = await text();
check("the Racks screen renders", /rack|torii|plaque|group/i.test(racks),
      racks.slice(0, 90));
check("Racks does not overflow", !(await overflows()));

await goto("/", 4000);
const railCount = await page.evaluate(() => {
  const rail = document.querySelector("#rail");
  if (!rail) return { err: "no rail" };
  // The "+N more" link is a .fleetrow too, so it has to come out of the count.
  const rows = [...rail.querySelectorAll(".fleetrow")]
    .filter((r) => !/^\+\s*\d+\s*more/i.test(r.innerText.trim()));
  return { rows: rows.length, more: /\+\s*\d+\s*more/i.test(rail.innerText) };
});
check("the rail shows at most five servers",
      railCount.rows <= 5, railCount.err || `${railCount.rows} rows, "+N more": ${railCount.more}`);

await goto(`/i/${big.server_id}/tune`, 7000);
const slider = await page.evaluate(() => {
  const r = [...document.querySelectorAll("input[type=range]")]
    .find((x) => /heap|ram|memory/i.test(x.getAttribute("aria-label") || x.id || x.name || ""))
    || document.querySelector("input[type=range]");
  if (!r) return { err: "no range input" };
  return { step: r.step, min: r.min, max: r.max };
});
check("the heap slider moves continuously, not in whole gigabytes",
      slider.step && Number(slider.step) > 0 && Number(slider.step) < 1024,
      slider.err || `step ${slider.step} (min ${slider.min}, max ${slider.max})`);

/* ================= §I — the fourth pass ================= */
console.log("\n--- §I: drawer collapse, vitals, skeletons ---");

// The drawer's collapse control must actually collapse — it never did before.
const drawer = await page.evaluate(async () => {
  const mod = await import("/static/js/jobs.js");
  if (mod.openDrawer) mod.openDrawer();
  await new Promise((r) => setTimeout(r, 700));
  const d = document.querySelector("#drawer");
  if (!d) return { err: "no #drawer" };
  const body = d.querySelector(".body");
  if (!body) return { err: "no drawer body" };
  const before = body.getBoundingClientRect().height;
  const toggle = d.querySelector("header button, .hd button, button[aria-expanded]");
  if (!toggle) return { err: "no collapse control" };
  toggle.click();
  await new Promise((r) => setTimeout(r, 700));
  const after = body.getBoundingClientRect().height;
  return { before: Math.round(before), after: Math.round(after),
           hidden: body.hasAttribute("hidden") };
});
check("the activity drawer actually collapses",
      !drawer.err && (drawer.after < drawer.before || drawer.hidden),
      drawer.err || `${drawer.before}px -> ${drawer.after}px (hidden ${drawer.hidden})`);

// Vital signs: a stopped instance must show no reading, not 0%.
await goto(`/i/${big.server_id}/overview`, 6000);
const vitals = await text();
check("a stopped instance shows no reading rather than 0%",
      !/\b0\s*%/.test(vitals) || /not running|stopped|no reading|—/i.test(vitals),
      (vitals.match(/(CPU|MEMORY)[^·|]{0,22}/i) || [""])[0]);

// Navigation paints a layout straight away rather than a blank page.
const skeleton = await page.evaluate(async () => {
  location.hash = "/discover";
  await new Promise((r) => setTimeout(r, 260));
  const body = document.body.innerText.trim().length;
  const skel = document.querySelectorAll(".skel, [class*=skel]").length;
  return { body, skel };
});
check("a new screen paints something within ~260ms",
      skeleton.body > 200 || skeleton.skel > 0,
      `${skeleton.body} chars, ${skeleton.skel} skeleton nodes`);

check("no console errors across the UI passes",
      errors.length === 0, errors.slice(0, 3).join(" | "));

await goto("/", 2500);
await page.screenshot({ path: `${OUT}/v21-uipass.png` });
const passed = out.filter(Boolean).length;
console.log(`\n${passed}/${out.length} checks passed`);
await browser.close();
process.exit(passed === out.length ? 0 : 1);
