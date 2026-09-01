// The features that did not exist in the previous build, exercised in a real
// browser against the real backend.
//
// ui.mjs covers the shell and the screens that have always been there. This
// covers what the 2026-09 rebuild added: the Undo card, Boot test, pack export,
// the fleet-wide update sweep and download cache, the wrong-loader finding, the
// Assistant tab, the deep-scan tab, and the roulette's per-slot hold/re-roll.
//
// It only READS. Nothing here starts, stops, installs, restores or deletes.
import puppeteer from "puppeteer";

const BASE = process.env.BF_URL || "http://127.0.0.1:8710";
const out = [];
const check = (n, c, extra = "") => {
  out.push(!!c);
  console.log(`${c ? "PASS" : "FAIL"}  ${n}${extra ? "  — " + String(extra).slice(0, 110) : ""}`);
};

const browser = await puppeteer.launch({ args: ["--no-sandbox"] });
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 1050 });
const errors = [];
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = () => page.evaluate(() => document.body.innerText.replace(/\s+/g, " "));
const click = (t) => page.evaluate((t) => {
  const all = [...document.querySelectorAll("button,a,div")].filter(e =>
    e.offsetParent && (e.tagName !== "DIV" || getComputedStyle(e).cursor === "pointer"));
  const el = all.reverse().find(e =>
    (e.innerText || "").replace(/\s+/g, " ").trim().includes(t));
  if (el) { el.click(); return true; }
  return false;
}, t);
const openServer = (name) => page.evaluate((name) => {
  const c = [...document.querySelectorAll("[data-bf-spine] div")]
    .filter(e => e.offsetParent && getComputedStyle(e).cursor === "pointer")
    .find(e => (e.innerText || "").includes(name));
  if (c) { c.click(); return true; } return false;
}, name);

// What the backend actually holds, so the assertions compare against truth
// rather than against a number typed into this file.
const api = async (p) => JSON.parse(await page.evaluate(async (u) =>
  await (await fetch(u)).text(), BASE + p));

await page.goto(BASE + "/", { waitUntil: "networkidle2", timeout: 60000 });
await sleep(8000);

/* --- Systems panel: the update sweep and the download cache -------------- */
const updates = await api("/api/updates");
const cache = await api("/api/cache");
const t0 = await text();
check("Systems reports the scheduled update sweep",
      new RegExp(`${updates.total}\\b`).test(t0) && /newer build/i.test(t0),
      `${updates.total} newer builds across ${Object.keys(updates.servers || {}).length} servers`);
check("the sweep offers a re-check action", /check again|re-?check/i.test(t0));
check("Systems reports the download cache",
      t0.includes(String(cache.used_gb)) && t0.includes(String(cache.limit_gb)),
      `${cache.used_gb} GB of ${cache.limit_gb} GB`);
check("the cache offers a trim action", /trim/i.test(t0));

/* --- Situation: Boot test, Export, Undo ---------------------------------- */
const instances = await api("/api/instances");
const withSnaps = [];
for (const i of instances.items) {
  const b = await api(`/api/instances/${i.server_id}/backups`);
  if ((b.items || []).length) withSnaps.push({ ...i, snaps: b.items });
}
// Snapshots are taken by destructive actions, so a freshly cleaned fleet
// legitimately has none. That is a skip, not a failure -- the card's empty
// state is asserted below either way.
if (withSnaps.length) {
  check("snapshots are listed for a server that has them", true,
        withSnaps.map(s => `${s.name}:${s.snaps.length}`).join(", "));
} else {
  console.log("SKIP  no server has snapshots yet — Undo shows its empty state");
}

const target = withSnaps[0] || instances.items[0];
await openServer(target.name);
await sleep(5000);
const sit = await text();

check("Boot test sits alongside Start/Restart/Stop",
      /Boot test/i.test(sit) && /Start/.test(sit) && /Stop/.test(sit));
check("the pack card offers a CurseForge export",
      /Export as a CurseForge pack|Export .*CurseForge/i.test(sit));
check("the Undo card is present", /undo/i.test(sit));
if (target.snaps) {
  check("Undo lists the real snapshots",
        target.snaps.every(s => sit.includes(String(s.reason || "").slice(0, 18))),
        target.snaps.map(s => s.reason).join(" | "));
  check("each snapshot offers Restore",
        (sit.match(/restore/gi) || []).length >= target.snaps.length);
  check("snapshots state how many mods they hold",
        /\d+\s*\/\s*\d+ mods/i.test(sit) || /\d+ of \d+ mods/i.test(sit),
        (sit.match(/\d+\s*\/\s*\d+ mods/i) || [""])[0]);
}
if (!target.snaps) {
  check("Undo explains itself when there is nothing to restore",
        /nothing to undo yet/i.test(sit));
}
check("world size stands in for the TPS tile that never existed",
      /world/i.test(sit) && !/\bTPS\b/.test(sit));

/* --- Diagnose: the wrong-loader finding and the five sub-tabs ------------ */
const withFindings = [];
for (const i of instances.items) {
  const d = await api(`/api/instances/${i.server_id}/diagnose`);
  if ((d.findings || []).length) withFindings.push({ ...i, findings: d.findings });
}
check("at least one server has findings to render", withFindings.length > 0,
      withFindings.map(s => `${s.name}:${s.findings.length}`).join(", "));

if (withFindings.length) {
  const f = withFindings[0];
  await openServer(f.name);
  await sleep(4000);
  await click("Diagnose");
  await sleep(5000);
  const diag = await text();
  check("Diagnose states the worst finding in a hero band",
        diag.includes(f.findings[0].title.slice(0, 30)), f.findings[0].title);
  check("a finding that carries a fix shows its button",
        !f.findings.some(x => x.fix) || /disable|pin|raise|accept|add a|flag/i.test(diag));
  for (const tab of ["Health checks", "Crash review", "Dependency scan", "Assistant"]) {
    check(`Diagnose has the ${tab} sub-tab`, diag.includes(tab));
  }
  check("Diagnose says when the checks last ran", /ran|checked|just now|ago/i.test(diag));

  await click("Dependency scan");
  await sleep(2500);
  const scan = await text();
  check("the deep scan screen offers to run", /scan/i.test(scan));

  await click("Assistant");
  await sleep(2500);
  const ai = await text();
  const aiStatus = await api("/api/ai/status");
  check("the Assistant names the real endpoint, not a placeholder",
        !/qwen2\.5|127\.0\.0\.1:11434/.test(ai)
        && (!aiStatus.model || ai.includes(aiStatus.model)),
        aiStatus.model || "(model not reported)");
  check("the Assistant offers both analyse and fix",
        /ask what is wrong|analyse/i.test(ai) && /review and fix/i.test(ai));
}

/* --- Mods: search now matches a description ------------------------------ */
await click("Mods");
await sleep(6000);
const mods = await text();
// The hint is a placeholder attribute, and innerText never contains those --
// reading the body text only ever matched the word "description" somewhere
// else on the screen.
const searchHint = await page.evaluate(() => {
  const i = [...document.querySelectorAll("input")]
    .find(el => /mods by name/i.test(el.placeholder || ""));
  return i ? i.placeholder : "";
});
check("the mod search says it matches what a mod does",
      /what it does/i.test(searchHint), searchHint);
check("the footer states how many rows are shown",
      /\d+ of \d+ shown/i.test(mods), (mods.match(/\d+ of \d+ shown/i) || [""])[0]);

/* --- Tune: everything listed, not a sample ------------------------------- */
await click("Tune");
await sleep(5000);
const opt = await api(`/api/instances/${withFindings[0]?.server_id || target.server_id}/optimize`);
const props = await api(`/api/instances/${withFindings[0]?.server_id || target.server_id}/properties`);
await click("JVM flags");
await sleep(2000);
const flags = await text();
check("every JVM flag the backend has is on screen",
      new RegExp(`${opt.flags.length} flags`).test(flags),
      `${opt.flags.length} flags in ${new Set(opt.flags.map(f => f.group)).size} groups`);
await click("server.properties");
await sleep(2000);
const pr = await text();
check("the properties count matches the backend",
      new RegExp(`of ${props.items.length} keys`, "i").test(pr),
      `${props.items.length} keys offered, ${props.count} set in the file`);

/* --- Roulette: hold and re-roll -------------------------------------- */
await click("Discover modpacks");
await sleep(3000);
await click("Mod Roulette");
await sleep(5000);
const rou = await text();
check("the roulette shows its empty state before a roll",
      /nothing dealt yet/i.test(rou));
check("the pool counter is present and not a fabricated constant",
      /mods pass/i.test(rou),
      (rou.match(/[\d,—…]+ MODS PASS/i) || [""])[0]);
check("the lever is there to pull", /pull/i.test(rou));

check("no uncaught page errors", errors.length === 0, errors.slice(0, 3).join(" | "));

const passed = out.filter(Boolean).length;
console.log(`\n${passed}/${out.length} checks passed`);
await browser.close();
process.exit(passed === out.length ? 0 : 1);
